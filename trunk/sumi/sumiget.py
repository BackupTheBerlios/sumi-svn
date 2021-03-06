#!/usr/bin/env python

# Created:20040117M
# By Jeff Connelly

# $Id$

# SUMI downloader, invoke: sumiget.py <transport> <server> <filename>
# See also: sumigetw.py

# Copyright (C) 2003-2006  Jeff Connelly <jeffconnelly@users.sourceforge.net>

# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 2
# of the License, or (at your option) any later version.
# 
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
# 
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301, 
# USA, or online at http://www.gnu.org/copyleft/gpl.txt .


import thread
import binascii
import base64
import socket
import struct
import signal
import sys
import os
import time
import libsumi
import pprint

from libsumi import *
from nonroutable import is_nonroutable_ip

def log(msg):
    print msg

def fatal(msg):
    log("FATAL ERROR: %s" % msg)
    os._exit(-1)

# Modules used by transports. Imported here so they compile in.
if sys.platform == 'win32':
    import win32api
    import mmap

# Uncomment to allow irclib
import irclib

input_lock = thread.allocate_lock()
#transport = "python -u transport/sumi-irc.py"
#global transport   
#transport = "mirc"

log("SUMI Client %s, Copyright (C) 2003-2006 Jeff Connelly" % SUMI_VER)
log("SUMI comes with ABSOLUTELY NO WARRANTY; for details see LICENSE")
log("This is free software, and you are welcome to redistribute it")
log("under certain conditions; see LICENSE for details.")
log("")

base_path = os.path.abspath(os.path.dirname(sys.argv[0])) + os.sep
config_file = base_path + "config.txt"
log("Using config file: %s" % config_file)

# Setup run-time path for loading transports
sys.path.append(os.path.realpath(os.path.dirname(sys.argv[0])))
#print "USING PATH = ", sys.path

#import transport.modmirc

global transports
transports = {}

# Make stdout unbuffered. Not sure if this is still needed.
real_stdout = sys.stdout
class unbuffered_stdout(object):
    def write(self, s):
        real_stdout.write(s)
        # Check to see if hash flush attribute, because the Blcakhole object
        # (used when ran as a GUI app with no console) does not
        if hasattr(real_stdout, "flush"): 
            real_stdout.flush()
sys.stdout = unbuffered_stdout()

# Used by transports to segment messages
def segment(nick, msg, max, callback):
    """Segment message 'msg' into 'max'-byte-sized message segments,
    calling callback(nick, message segment) for each segment. Continued
    messages begin with ">"; the last segment has no ">".
    
    Used by transports. Useful to overcome length limitations of a transport,
    such as IRC's privmsg. If a transport has length limitations, it should
    segment on the maximum message length (hopefully larger than 5 as in this
    example) in its sendmsg() and pass sendmsg_1(), by convention, as the
    callback, to perform the actual message sending.

    Segment into messages of 5 byte or fewer, calling keep() on each part:

>>> a=[]
>>> def keep(nick, msg):
...     a.append((nick, msg))
...
>>> segment("nick", "hello world", 5, keep)
>>> a
[('nick', '>hell'), ('nick', '>o wo'), ('nick', 'rld')]
>>>"""
    n = 0
    prefix = ">"
    max -= len(prefix)
    while len(msg[n:n + max]):
        if n + max >= len(msg):
            max += len(prefix)
            prefix = ""
        callback(nick, prefix + msg[n:n+max])
        n += max

class Client(object):
    def __init__(self):
        log("Loading config...")
        # Mode "U" for universal newlines, so \r\n is okay
        self.config = eval("".join(file(config_file, "rU").read()))
        libsumi.cfg = self.config
        libsumi.log = log
        log("OK")

        # Make sure file is writable
        # os.access W_OK is not reliable across SMB shares
        #try:
        #    file(config_file, "a")
        #except IOError:
        #    log("The config file is not writable, but it must be")
        #    log("in order to save settings. Please correct.")
        #    raise SystemExit
        # Problem: reporting to GUI, excluded for now

        # Now performed manually in sumigetw
        #self.validate_config()
        self.senders = {}

        self.sockets = []

        self.set_callback(self.default_cb)   # override me please

    def save_lost(self, u, finished=False):
        r'''Write the resume file for transfer from the user.

The saved file will clobber the previous resume file, but will contain 
enough information to resume the transfer from its current state. The data is
written to the filehandle from the "fs" key:

>>> import tempfile
>>> u = {}
>>> u["fs"] = tempfile.TemporaryFile()
>>> u["lost"] = {1:True,2:True,5:True}
>>> u["at"]=10
>>> u["transport"] = "debug"
>>> u["nick"] = "nick"
>>> u["filename"] = "#1"
>>> c.save_lost(u)
>>> u["fs"].seek(0)
>>> u["fs"].read()
'nick=nick\ntransport=debug\nat=10\nlost=1-2,5\nfilename=#1\n'
>>>


If "fs" is not present, the resuming file will not be written:

>>> c.save_lost({"nick":"someone"})
Not saving resuming file for someone
>>>

    The 'finished' parameter is set if the file is complete and the resume
    file should be closed.
'''

        fs = u.get("fs", None)
        if fs is None or fs.closed:
            log("Not saving resuming file for %s,u=%s" % (u["nick"], u))
            return

        fs.seek(0)    
        fs.truncate()    # Clear
        fs.flush()

        # Overwrite with new lostdata
        d = {}
        d["lost"] = pack_range(u.get("lost", {}).keys())
        d["at"] = str(u["at"])

        # For torrent-like invoking
        d["transport"] = u["transport"]
        d["nick"] = u["nick"]
        d["filename"] = u["filename"] 

        # Conditionally save these keys
        export = ["irc_server", "irc_port", "irc_channel",
            "irc_channel_password", "multicast_group"]
        for k in export:
            if u.has_key(k):
                d[k] = u[k]

        # TODO: better file format
        fs.write(pack_dict(d))
        fs.flush()

        if finished:
            # Leave group associated with transfer
            if u.has_key("multicast_group"):
                for s in self.sockets:
                    self.mcast_op(s, socket.IP_DROP_MEMBERSHIP,
                        u["multicast_group"])
            #fs.close()

    def prefix2user(self, prefix):
        """Find user that is associated with the random prefix; which is the
        only way to identify the source. Return None if no user.

        O(n) performance, time-wise.

>>> c.senders["alice"]    = {"prefix": 'ABC', "nick": 'alice'}
>>> c.senders["bob"]      = {"prefix": 'DEF', "nick": 'bob'}
>>> c.prefix2user("ABC") == c.senders["alice"]
True
>>> c.prefix2user("DEF") == c.senders["bob"]
True
>>> c.prefix2user("XYZ") is None
True
>>>"""
        # The data structures aren't setup very efficiently.
        for x in self.senders:
            if self.senders[x].get("prefix") == prefix:
                return self.senders[x]
        return None

    def load_transfer(self, filename):
        """Load a .sumi file, returning a dictionary for 'u'."""
        log("Loading transfer from %s" % filename)

        raw = file(filename, "rU").read()
        if len(raw) == 0: 
            log("%s couldn't be loaded, no data" % filename)
            return None

        d_all = unpack_dict(raw)
        
        # Allow only certain fields for security
        allow = ["transport", "nick", "filename", "irc_server",
            "irc_port", "irc_channel", "irc_channel_password",
            "multicast_group"]
        d = {}
        for k in d_all.keys():
            if k in allow:
                d[k] = d_all[k]
    
        return d

    def setup_resuming(self, u, lostdata, at):
        """Setup data structures to resume a file.

        lostdata is a packed range string of missing packets.
        at is a string of the position to resume at in decimal.
        
        Return True if file is complete, False to begin transfer."""

        u["at"] = int(at)

        log("RESUMING AT %s" % u["at"])
        log("IS_RESUMING: LOST: %s" % lostdata)#pack_range(map(int, lostdata)))
        u["lost"] = {}
        for x in unpack_range(lostdata):
            try:
                u["lost"][int(x)] = 1
            except None:#ValueError:
                log("setup_resuming: ValueError: %s" % x)
                pass    # don't add non-ints
        log("LOADED LOSTS: %s" % pack_range(u["lost"].keys()))

        # Initialize the rwin with empty hashes, mark off missings
        u["rwin"] = {}
        for x in range(1, u["at"] + 1):
            u["rwin"][int(x)] = 1   # received 

        for L in u["lost"]:    # mark losses
            u["rwin"][int(L)] = 0

        #print "RESUME RWIN: ", u["rwin"]

        # Total bytes received is file length minus lost packet sizes.
        # (Note: MTU may be inconsistant across users, so resuming files
        # cannot be shared across users with different MTUs.)
        # Number of bytes received = (offset - lost packets) * segment size
        u["bytes"] = (u["at"] - len(u["lost"])) * self.mss

        if u["bytes"] >= u["disk_size"]:
            log("*** Loaded complete file")
            # will be called back in request() countdown loop
            return False

        # Files don't store statistics like these
        u["all_lost"] = []  # blah
        u["rexmits"] = 0

        return True

    def setup_non_resuming(self, u):
        """Setup data structures for a new file. Returns True to begin."""
        # Initialize
        u["at"] = 0
        u["rexmits"] = 0
        u["all_lost"] = []
        u["bytes"] = 0  # bytes received
        u["lost"] = {}    # use: keys(), pop()..
        # RWIN is a list of all the packets, and if they occured (0=no),
        # incremented each time a packet of that seqno is received. Since
        # Python arrays don't automatically grow with assignment, a hash
        # is used instead. If "rwin" was an array, [], missed packets would
        # cause an IndexError. See
        #http://mail.python.org/pipermail/python-list/2003-May/165484.html
        # for rationale and some other class implementations
        u["rwin"] = {}

        return True

    def setup_file(self, u):
        """Setup the file to save to. Return True to proceed."""
        u["start"] = time.time()

        fn = self.config["dl_dir"] + os.path.sep + u["filename"]
        log("Opening %s for %s..." % (fn, u["nick"]))

        u["final_fn"] = fn

        # In FEC mode, save codewords to fn.fec then decode to fn
        if u["control_proto"] == "fec":
            u["wire_fn"] = fn + ".fec"
        else:
            u["wire_fn"] = fn

        # These try/except blocks try to open the file rb+, but if
        # it fails with 'no such file', create them with wb+ and
        # open with rb+. Good candidate for a function!
        try:
            u["fh"] = file(u["wire_fn"], "rb+")
        except IOError:
            file(u["wire_fn"], "wb+").close()
            u["fh"] = file(u["wire_fn"], "rb+")
        log("open")

        # For FEC, codewords will be decoded to fn
        if u["control_proto"] == "fec":
            u["fh_final"] = file(u["final_fn"], "wb+")

        # Open a new resuming file (create if needed)
        try:
            u["fs"] = file(fn + ".sumi", "rb+")
            is_resuming = 1  # unless proven otherwise
        except IOError:
            file(fn + ".sumi", "wb+").close()
            u["fs"] = file(fn + ".sumi", "rb+")
            is_resuming = 0   # empty resume file, new download

        # Lost data format: lostpkt1,lostpkt2,...,current_pkt
        lostdata = ""

        # Check if the data file exists, and if so, resume off it
        if os.access(fn, os.R_OK):
            # The data file is readable, read lost data 
            #lostdata = u["fs"].read().split(",")
            d = unpack_dict(u["fs"].read())
            if not d.has_key("lost") and not d.has_key("at"):
                is_resuming = 0
                log("Since no lost+at key, not resuming")
            else:
                lostdata = d["lost"]
                at = d["at"]
                is_resuming = 1
        else: 
            is_resuming = 0     # Can't read data file, so can't resume

        # Need at least an offset to resume...
        log("LEN LOSTDATA=%s" % len(lostdata))#,"and lostdata=",lostdata

        #is_resuming=0#FORCE

        # Setup lost packets
        if is_resuming:   # this works
            return self.setup_resuming(u, lostdata, at)
        else:
            return self.setup_non_resuming(u)

    def handle_auth(self, u, prefix, addr, data):
        """Handle the authentication packet."""
        log("Got auth packet from %s for %s" % (addr,u["nick"]))
        
        # Remove CRC from auth packet; its hashed before its filled in
        data = data[:7] + "\0\0\0\0" + data[11:]

        if u.has_key("crypto_state"):
            g = time.time()
            d3 = g - u["sent_req2"]
            if d3 >= INTERLOCK_DELAY:  # really 2*INTERLOCK_DELAY
                log("WARNING: POSSIBLE MITM ATTACK! %s seconds is too long."
                        % d3)
                log("Your request may have been intercepted.")
                # only a warning because first data packet should catch it

            # Setup data encryption (CTR & package ECB)
            u["sessiv"] = inc_str(u["sessiv"])
            from AONT import AON
            u["aon"] = AON(get_cipher(), get_cipher().MODE_ECB)

            #log("Decrypted payload: %s" % ([data],))
        
        if u.get("crypt_data"):
            # Decrypt payload, THEN hash. Note that crypt_data enables auth
            # pkt to be encrypted, since it goes over the data channel.
            u["ctr"] = u["data_iv"]
            log("DEC AP WITH: %s" % u["ctr"])
            assert len(data[SUMIHDRSZ:]) % get_cipher().block_size == 0, \
                    "Length of data is %s, not multiple of cipher bs %s" % (
                            len(data[SUMIHDRSZ:]), get_cipher().block_size)
            data = data[0:SUMIHDRSZ] + u["crypto_obj"].decrypt(
                    data[SUMIHDRSZ:])

        hashcode = b64(hash128(data))

        # File length, new prefix, flags, filename
        i = SUMIHDRSZ
        disk_size_str, i = take(data, 4, i)
        wire_size_str, i = take(data, 4, i)
        new_prefix, i = take(data, 3, i)
        flags_str, i = take(data, 1, i)

        u["disk_size"], = struct.unpack("!I", disk_size_str)
        u["wire_size"], = struct.unpack("!I", wire_size_str)
        log("SIZE:disk=%s, wire=%s" % (u["disk_size"], u["wire_size"]))
 
        if u["control_proto"] == "fec":
            # Calculate last K (# of data packets) for FEC encoded group
            log("FEC_K=%s" % u["fec_k"])
            leftover = u["disk_size"] % (u["fec_k"] * self.mss)
            lastsize = leftover + (self.mss - leftover % self.mss)
            u["fec_last_k"] = lastsize / self.mss
            log("FEC_LAST_K:%s" % u["fec_last_k"])

            # This applies to the last group
            u["fec_last_group"] = int(math.ceil(u["disk_size"] / 
                (u["fec_k"] * self.mss * 1.0)))

        assert len(new_prefix) == 3, "Missing new_prefix in auth packet!"
        flags = ord(flags_str)
        log("FLAGS:%s" % flags)
        u["mcast"] = flags & 1
        if u.has_key("crypto_state"):
            recvd_hash, i = take(data, 20, i)
            derived_hash = u["nonce_hash"]
            if recvd_hash != derived_hash:
                log("Server verification failed! %s != %s" % (
                        ([recvd_hash], [derived_hash])))
                self.clear_server(u)
                return
            log("Server verified: interlock nonce matches auth pkt nonce")
        else:
            log("Skipping server verification, crypto disabled")

        u["file_hash"], i = take(data, 20, i)
    
        filename = data[i:data[i:].find("\0") + i]

        u["filename"] = filename
        log("Filename: <%s>" % filename)

        # Server can change prefix we suggested (negotiated).
        log("OLD PREFIX: %02x%02x%02x" % (tuple(map(ord, u["prefix"]))))
        log("NEW PREFIX: %02x%02x%02x" % (tuple(map(ord, new_prefix))))

        if new_prefix != u["prefix"]:
            # Most likely, switching because server is already sending the 
            # file (multicasting for example)
            log("Switching to a new prefix!")
        u["prefix"] = new_prefix

        self.callback(u["nick"], "info", u["disk_size"], 
            b64(prefix), filename, 
            u["transport"], 
            self.config["data_chan_type"])

        new_mss = len(data) - SUMIHDRSZ

        assert new_mss <= self.mss, \
                "Auth packet MSS too large: %s > %s" % (new_mss, self.mss)

        if self.mss != new_mss:
            # This is a temporary downgrade, since the server might have the
            # MTU limitation, not us.
            log("Downgrading MSS %s->%s" % (self.mss, new_mss))

            # If using crypto, MSS normally rounded to block size
            if not u["crypt_data"]: 
                log("If this happens consistently, considering lowering MTU.")

            self.mss = new_mss
            if self.mss < 256:
                log("MSS is extremely low (%d), quitting" % self.mss)
                sys.exit(-1)

        # Open the file and set it up
        if not u.has_key("fh"):  #  file not open yet
            proceed = self.setup_file(u)
            if not proceed:
                # Not given the go, probably file was finished and no
                # transfer needs to begin
                return

        # Tell the sender to start sending, we're ok
        # Resume /after/ our current offset: at + 1.
        # And in sumi auth, **"m" is MSS**
        log("Sending sumi auth")
        auth = pack_args({"m":self.mss,
               "s":addr[0], "h":hashcode, "o":u["at"] + 1})
        if u.has_key("crypto_state"):
            #u["sessiv"] = inc_str(u["sessiv"])
            auth = b64(self.encrypt(u, auth))
            log("Encrypted sumi auth: %s" % auth)
        else:
            auth = "sumi auth " + auth
        self.sendmsg(u, auth)

        if u["control_proto"] == "nak":
            self.check_naks()    # instant update
        else:
            self.check_finished(u)

        return

    def undigest_file(self, u):
        """After a file is complete, undigest (unpackage) it."""
        # Something is broken
        print [u["aon"].undigest(d)]

    def handle_first(self, u, seqno):
        """Handle the first packet from the server."""
        u["start_seqno"] = seqno
        log("FIRST PACKET: %s" % seqno)

        u["got_first"] = True

        if u.has_key("crypto_state"):
            # Make sure first data packet is received soon enough
            g = time.time()
            d4 = g - u["sent_req2"]
            if d4 >= 2*INTERLOCK_DELAY-0.1:
                log("POTENTIAL MITM ATTACK DETECTED--DELAY TOO LONG. %s"%d4)
                os._exit(-1)
                return
            else:
                log(":) No MITM detected")
        self.callback(u["nick"], "recv_1st")

    def handle_data_aont(self):
        """Handle all-or-nothing transform when data is received.
        Currently broken."""

        # With crypto (AONT), last packet goes OVER the end of the file,
        # specifically, by one block--the last block, encoding K'.
        if offset + payloadsz > u["disk_size"]:
            u["got_last"] = True

        # Inner "crypto": ECB package mode, step 1 (gathering)

        if u.has_key("got_last"):
            # Pass last block to gather_last(), then can decrypt
            last_block = data[-get_cipher().block_size:]
            pseudotext = data[0:-get_cipher().block_size]

            u["aon"].gather(pseudotext)
            u["aon"].gather_last(last_block)

            u["aon_last"] = last_block

            log("Gathered last block!")
            u["can_undigest"] = True
        else:
            u["aon"].gather(pseudotext, ctr)

        # Save data in file and unpackage after finished
        print "LEN:%s vs. %s" % (len(data), len(pseudotext))
        #data = pseudotext


    def handle_data(self, u, prefix, addr, seqno, data):
        """Handle data packets."""

        # Prefix has been checked, seqno calculated, so just get to the data
        data = data[SUMIHDRSZ:]

        payloadsz = len(data)

        if not u.has_key("got_first"):
            self.handle_first(u, seqno)

        # All file data is received here

        u["seqno"] = seqno
        u["last_msg"] = time.time()
        offset = (seqno - 1) * self.mss

        if u["rwin"].get(seqno,0) >= 2:
            log("(DUPLICATE PACKET %d, IGNORED)" % seqno)
            return

        if u["control_proto"] == "ack":
            # If ACK protocol is used (stop-and-wait), request next immediately
            self.sendmsg(u, "k%d" % (seqno + 1))
            self.save_lost(u)
        if u["control_proto"] == "fec":
            # Reassemble if can (erasures <= n-k <==> received >= k)
            # Check each unreassembled set of FEC_CODEWORD_COUNT packets
            group = seqno / FEC_CODEWORD_COUNT
            idx = seqno % FEC_CODEWORD_COUNT

            if u["fec_group"].get(group):
                log("Redundant packet %s for %s received and ignored" % 
                    (seqno, group))
                return

            # Start and end seqnos for this group. Note that we may have not
            # yet received the ending seqno!
            start = group * FEC_CODEWORD_COUNT
            end = start + FEC_CODEWORD_COUNT 

            # Number of data packets per n-packet group
            if group == u["fec_last_group"]:
                k = u["fec_last_k"]
                log("Last group! K=%s" % k)
            else:
                k = u["fec_k"]

            # Do we have enough packets to recover this group?
            # This occurs if: erasures <= n-k
            # which is equivalent to received_packets >= k
            # First check if idx >= k because if fewer than k packets were
            # received, then the group is definitely not recoverable. If more
            # than k were received, count gaps (expensive operation) and see
            # if there is enough
            log("(#%s,n=%s,group=%s) idx=%s, k=%s, idx-gaps=%s, gaps=%s" % (
                seqno,FEC_CODEWORD_COUNT,group,
                idx,k,idx-len(self.find_gaps(u,start,seqno)),len(self.find_gaps(u,start,seqno))))
            if idx >= k and \
                    idx - len(self.find_gaps(u, start, seqno)) >= k:
                print "OK - have enough packets to recover group %s!" % group
                rs = reedsolomon.Codec(FEC_CODEWORD_COUNT, k)

                # Gather packets and erasures for decoding
                pkts = []
                erasures = []
                for check_seqno in xrange(start, end):
                    # If received up to here and packet was received...
                    if seqno < end and u["rwin"].has_key(check_seqno):
                        u["fh"].seek((check_seqno - 1) * self.mss)
                        p = u["fh"].read(self.mss)
                        pkts.append(p)
                        assert len(p) == self.mss, \
                            "fec: for seqno=%s, read %s, not mss=%s" \
                                % (check_seqno, len(p), self.mss)
                    else:
                        # Specify erasures because the FEC code can recover
                        # more erasures than errors. Fill with zeroes up to
                        # MSS by reedsolomon module requirement (all packets
                        # must be same size), but it will be ignored otherwise.
                        pkts.append("\0" * self.mss)
                        erasures.append(check_seqno % FEC_CODEWORD_COUNT)

                # Recover
                log("erasures(%s,k=%s)=%s" % (len(erasures), k, erasures))
                decoded, corrections = rs.decodechunks(pkts, erasures)
                log("DECODED %s to %s (%s corrections)" % (
                        len(pkts), len(decoded), len(corrections)))
                u["fec_group"][group] = True

                # Save
                u["fh_final"].seek(group * FEC_CODEWORD_COUNT)
                u["fh_final"].write("".join(decoded))

                u["bytes"] += len("".join(decoded))

                print "done yet? %s >=? %s" % (u["bytes"], u["wire_size"])
                if u["bytes"] >= u["wire_size"]:
                    self.finish_xfer(u)

                # TODO: check if previous group was recovered; if not, ARQ

            # TODO: ARQ end of group and can't reassemble
            # TODO: nothing if not yet received packets
            pass

        #print "THIS IS RWIN: ", u["rwin"]

        if not u.has_key("crypto_state"):
            # Without crypto (AONT), last packet is when completes file
            if offset + payloadsz >= u["wire_size"]:
                u["got_last"] = True

        if u["crypt_data"]:
            data = handle_data_crypto(u, seqno)
            # Outer crypto: CTR mode
            u["ctr"] = (calc_blockno(seqno, self.mss) + u["data_iv"])
            #log("CTR:pkt %s -> %s" % (seqno, u["ctr"]))
            data = u["crypto_obj"].decrypt(data)

        # XXX: broken
        if False and u.has_key("crypto_state"):
            handle_data_aont(u, seqno, offset, payloadsz, data)

        # New data (not duplicate, is cleartext) - add to running total
        if u["control_proto"] != "fec":
            u["bytes"] += len(data) 
        #u["bytes"] = u["fh"].tell() #- (self.mss * len(u["lost"].keys()))#XXX

        u["fh"].seek(offset)
        u["fh"].write(data)

        # Mark down each packet in our receive window if NAK or FEC
        # Do this only AFTER writing the file to disk, so it can be read later
        if u["control_proto"] in ("nak", "fec"):
            # Increment number of packets received from this seqno 
            u["rwin"][seqno] = u["rwin"].get(seqno, 0) + 1

        if u.has_key("can_undigest"):
            self.undigest_file(u)

        # Note: callback called every packet; might be too excessive
        self.callback(u["nick"], "write", u["bytes"], u["disk_size"], addr)

        # Check previous packets, see if they were lost (unless first packet)
        if u["control_proto"] == "nak":
            self.check_losses(u, seqno, data)
        elif u["control_proto"] == "ack":
            if u.has_key("got_last"):
                self.finish_xfer(u)

    def find_gaps(self, u, start, end):
        """Return list of missing packets from start to end in u."""
        missing = []
        for seqno in xrange(start, end):
            if not u["rwin"].has_key(seqno):
                missing.append(seqno)
        return missing

    def check_losses(self, u, seqno, data):
        """Check for packet losses for NAK protocol."""
        if seqno > 1:
            i = 1 
            # Neat little algorithm. Work backwards, searching for gaps.
            # check_losses() is called when each packet is received, so only
            # up until the first received packet is checked.
            while seqno - i >= 0:
                #print "?? ", seqno-i
                if not u["rwin"].has_key(seqno - i):
                    u["lost"][seqno - i] = 1
                    u["all_lost"].append(str(seqno - i))
                    i += 1
                else:
                    break  # this one wasn't lost, so already checked


           # Fill in u["lost"] and u["all_lost"] from seqno
            self.find_gap(u, seqno)

            # Re-request packets even if using multicast
            #if u["mcast"]:
            #    log("using mcast, so not re-requesting these lost pkts")
            #    # we'll get these packets next time around 

            if u.get("lost", {}).has_key(seqno):
                u["lost"].pop(seqno)
                log("Recovered packet %s %s" % (seqno, len(u["lost"])))
                u["rexmits"] += 1
                log("(rexmits = %s" % u["rexmits"])
                self.callback(u["nick"], "rexmits", u["rexmits"])
                #check_naks()   # Maybe its all we need
            if u.has_key("got_last"):
                log("LAST PACKET: %d =? %d" % (len(data), self.mss))
                # File size is now sent in auth packet so no need to calc it
                #u["disk_size"] = u["fh"].tell()
                # We got to the end of the file, but with the NAK control
                # protocol (which continously sends packets) may have left
                # gaps in the file. Check it.
                self.check_naks()

        if u:
            self.save_lost(u)  # for resuming

        if u and u.get("lost"):
            self.callback(u["nick"], "lost", u["lost"].keys())
            #print "These packets are currently lost: ", u["lost"].keys()
        else:
            self.callback(u["nick"], "lost", ())

    def handle_packet(self, data, addr):
        r"""Handle received packets.

        Packets less than SUMIHDRSZ bytes are discarded and return False:

>>> c.handle_packet("foo",())
Short packet: 3 bytes from ()
False
>>>
        Users are looked up by their prefix for packets of sufficient size.
        Unrecognized prefixes return None and are discarded:

>>> c.handle_packet("AAA\x00\x00\x00\x01\xdc\xeb\xa0\x89hello",())
DATA:UNKNOWN PREFIX! 414141 16 bytes from ()
>>>

        Data from aborted transfers is discarded and returns False. 
        Data which fails CRC check is also discarded and returns False.

        Otherwise, 'retries' is zeroed, 'last_msg' is set to the timestamp,
        and 'at' is set to the received sequence number. Finally the data
        is passed to handle_auth if seqno is 0, handle_data if not.
"""

        if len(data) < SUMIHDRSZ:
            log("Short packet: %s bytes from %s" % (len(data), addr))
            return False

        prefix = data[:3] 
        seqno, = struct.unpack("!I", data[3:7])  # 4-bytes
        checksum = data[7:11]

        if not check_crc(data):
            if seqno == 0:
                # TODO: Re-request auth packet automatically. Currently, bail.
                u = self.prefix2user(prefix)
                if u:
                    self.callback(u["nick"], "auth_crc_fail")
                    #self.clear_server(u)
                    # Stop countdown and abort-on-close
                    u["handshake_error"] = True

            # check_crc() will detailed info if it fails, but
            # TODO: callback so GUI can show CRC errors
            return False

        u = self.prefix2user(prefix)
        if not u:
            p = "%02x%02x%02x" % (tuple(map(ord, prefix)))
            # On Win32 this takes up a lot of time
            log("DATA:UNKNOWN PREFIX! %s %s bytes from %s"
                    % (p,len(data),addr))
            return None

        if u.get("aborted"):
            log("Ignoring aborted data")
            return False

        u["retries"] = 0   # acks worked

        u["last_msg"] = time.time()

        # Last most recently received packet, for resuming
        u["at"] = seqno 

        log("PACKET: %s (%s)" % (seqno, len(data)))

        # Sequence number is 3 bytes in the SUMI header in network order
        # (so a null can easily be prepended for conversion to a long),
        # this used to be partially stored in the source port, but PAT--
        # Port Address Translation--closely related to NAT, can mangle 
        # the srcport. So srcport isn't used at all by SUMI.
        if seqno == 0:       # all 0's = auth packet
            return self.handle_auth(u, prefix, addr, data)
        else:
            return self.handle_data(u, prefix, addr, seqno, data)

    def thread_ack_timer(self):
        """Every ACK_PKT_TIMEOUT seconds, check if need to re-request packet."""

        if self.config["control_protocol"] != "ack":
            return   # no-op

        while True:
            # symtable gets clobbered in some cases
            import time

            if not ACK_PKT_TIMEOUT: ACK_PKT_TIMEOUT = 0.1

            time.sleep(ACK_PKT_TIMEOUT)

            for x in self.senders:
                u = self.senders[x]
                # If times out, send NAK. Server doesn't handle timeouts, we do
                if u.has_key("seqno") and \
                        time.time() - u["last_msg"] >= ACK_PKT_TIMEOUT:
                    self.sendmsg(u, "k%d" % (u["seqno"] + 1))


    def thread_nak_timer(self):
        """Every RWINSZ seconds, send a nak of missing pkts up to that point."""
        if self.config["control_protocol"] != "nak":
            return   # no-op

        try:
            while True:
                time.sleep(self.rwinsz)
                #log("!! Calling timer")
                self.check_naks()
        except Exception, x:
            log("thread_nak_timer exception: %s" % x)

    def check_finished(self, u):
        """Check if transfer is finished, if so, finish it."""

        # Should never have more bytes than are in the file, but this bug
        # isn't yet fixed. TODO: fix it. Something to do w/ resends.
        #assert u["bytes"] <= u["disk_size"], \
        #        "check_finished(%s), bytes=%s > size=%s" % (u["nick"], 
        #                u["bytes"], u["disk_size"])

        # Old way: EOF if nothing missing and got_last
        #if (len(u["lost"]) == 0 and 
        #    usenders[x].has_key("got_last")):
        #    return self.finish_xfer(x) # there's nothing left, we're done!
        # New way: EOF if total bytes recv >= size and nothing missing
 
        if u["bytes"] >= u["disk_size"] and not u.get("lost"):
            return self.finish_xfer(u)


    def check_naks(self):
        """Acknowledge to all senders and update bytes/second.
        Called every RWINSZ seconds by thread_nak_timer()."""
        tmp_senders = self.senders.copy()
        for x in tmp_senders:
            u = self.senders[x]
           
            # If aborted or haven't got first packet, don't ack
            if u.get("aborted") or not u.get("got_first"):
                #log("Skipping u since aborted/not first")
                continue

            if not u.has_key("lost"):  # not xfering yet
                u["retries"] = 0   # initialize
                #log("Skipping u since no lost")
                continue

            # Update rate display
            if u.has_key("bytes"):
                if u.has_key("last_bytes"):
                    bytes_per_rwinsz = u["bytes"] - u["last_bytes"]
          
                    rate = float(bytes_per_rwinsz) / float(self.rwinsz) 
                    # rate = delta_bytes / delta_time   (bytes arrived)
                    # eta = delta_bytes * rate          (bytes not arrived)
                    if rate != 0:
                        eta = (u["disk_size"] - u["bytes"]) / rate
                    else:
                        eta = 0
                    # Callback gets raw bits/sec and seconds remaining
                    self.callback(x, "rate", rate, eta)
                    u["last_bytes"] = u["bytes"]
                else:
                    u["last_bytes"] = u["bytes"]

            self.check_finished(u)
            try:
                # Some missing packets, finish it up
    
                if len(pack_range(u.get("lost", {}).keys())) > 100:
                    log(pack_range(u["lost"].keys()))
                    log("WARNING: Excessive amount of packet loss!")
                    #raise SystemExit

                # Join by commas, only lost packets after start_seqno
                alost = u.get("lost", {}).keys()
                log("ALOST1: %s" % len(alost))
                if u.has_key("start_seqno"):
                    ss = u["start_seqno"]
                else:
                    log("WARNING: NO START_SEQO SET!")
                    ss = 0

                # Previously, stopped re-requesting packets below start_seqno,
                # but not anymore...
                # NOTE: _y isn't localized here! Don't use x!
                # won't request any lost packets with seqno's below start_seqno
                #alost = [ _y for _y in alost if _y >= ss ]

                log("ALOST2 (ss=%s): %s" % (ss, len(alost)))
                #lost = ",".join(map(str, alost))
                # Compressed NAKs
                lost = pack_range(alost)

                # Send NAKs if protocol is for it
                if u.get("control_proto") == "nak":
                    # Compress by omitting redundant elements to ease bandwidth
                    if self.rwinsz_old == self.rwinsz and lost == "":
                        self.sendmsg(u, "n")
                    elif lost == "":
                        self.sendmsg(u, "n%d" % self.rwinsz)
                    else:
                        self.sendmsg(u, ("n%d," % self.rwinsz) + lost)

                if u.has_key("retries"):
                    u["retries"] += 1

                if u.get("retries") > 3:
                    log("%s exceeded maximum retries (3), cancelling" % x)
                    self.senders.pop(x)
                    self.callback(x, "timeout")

                self.rwinsz_old = self.rwinsz# TODO: update if changes..but need
                                          # to give the win on first(right)

            except None:   # sender ceased existance
                pass
        return None

    def finish_xfer(self, u):
        """Finish the file transfer. Save the lost data, notify the 
        server and callback."""

        # Nothing lost anymore, update. Saved as complete.
        log("DONE - UPDATING")
        self.save_lost(u, finished=True)

        self.sendmsg(u, "sumi done")

        duration = time.time() - u.get("start", time.time())
        u["fh"].close()
        self.callback(u["nick"], "xfer_fin", duration, u["disk_size"], 
              u["disk_size"] / duration / 1024, 
              u.get("all_lost", []))

        self.callback(u["nick"], "hash_start")

        # Verify the hash
        def update_hash(n):
            self.callback(u["nick"], "hashing", n)

        log("Hashing...")
        h1 = hash_file(self.config["dl_dir"] + os.path.sep + u["filename"],
                update_hash)
        h2 = u["file_hash"]
        log("-> %s vs %s" % ([h1], [h2]))
        if h1 != h2:
            log("HASH FAILED!")
            self.callback(u["nick"], "hash_fail")
            raise SystemExit
      
        else:
            self.callback(u["nick"], "hash_ok")

        #print "Transfer complete in %.6f seconds" % (duration)
        #print "All lost packets: ", u["all_lost"]
        #print str(u["disk_size"]) + " at " + str(
        #     u["disk_size"] / duration / 1024) + " KB/s"
        self.senders.pop(u["nick"])    # delete the server key

        # Don't raise SystemExit
        #sys.exit(0) # here now for one file xfer per program

    def recv_packets(self):
        """Receive anonymous packets."""
        log("THREAD 1 - PACKET RECV")
        if self.config["dchanmode"] == "socket":
            if self.config["data_chan_type"] == "u":
                self.server_udp()
            elif self.config["data_chan_type"] == "e":
                self.server_icmp()
            elif self.config["data_chan_type"] == "i":
                self.server_icmp()
            else:
                log("data_chan_type invalid, see config.html" 
                    "(dchanmode=socket)")
                sys.exit(-2)
        elif self.config["dchanmode"] == "pcap":
            if self.config["data_chan_type"] == "u":
                self.server_udp_PCAP()
            else:
                log("data_chan_type invalid, see config.html"
                        "(dchanmode=pcap)")
                sys.exit(-3)
        else:
            log("*** dchanmode invalid, set to socket or pcap")
            sys.exit(-4)

    
    def setup_socket(self, s):
        """Set socket options on s."""
        # Allow reusing local addresses
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)

        self.sockets.append(s)

    def mcast_op(self, s, op, group):
        """Add or drop a multicast group on s.
        op is socket.IP_(ADD/DROP)_MEMBERSHIP."""
        assert op in [socket.IP_ADD_MEMBERSHIP, socket.IP_DROP_MEMBERSHIP], \
                "mcast_op: %s isn't add or drop" % op

        word = {socket.IP_ADD_MEMBERSHIP:  "Joining",
                socket.IP_DROP_MEMBERSHIP: "Leaving"}[op]

        iface = self.config["mcast_iface"]

        log("%s multicast %s on %s" % (word, group, iface))

        try:
            s.setsockopt(socket.SOL_IP, socket.IP_ADD_MEMBERSHIP,
                    socket.inet_aton(group) + socket.inet_aton(iface))
        except socket.error, e:
            if op == socket.IP_ADD_MEMBERSHIP:
                fatal("Couldn't join mcast group %s: %s" % (group, e))
            else:
                log("Warning: couldn't leave mcast %s: %s" % (group, e))

    def server_icmp(self):
        """Receive ICMP packets. Requires raw sockets."""

        #thread.start_new_thread(self.server_udp, (self,))
        thread.start_new_thread(self.wrap_thread, (self.server_udp, (self,)))
        #print "UID=", os.getuid()
        sock = socket.socket(socket.AF_INET, socket.SOCK_RAW, 
                             socket.IPPROTO_ICMP)

        self.setup_socket(sock)

        log("ICMP started.")   # At the moment, needs to be ran as root
        sock.bind((self.localaddr, 0))
        while True:
            (data, addr) = sock.recvfrom(65535)
            data = data[20 + 8:]     # IPHDRSZ + ICMPHDRSZ, get to payload
            self.handle_packet(data, addr)

    def server_udp(self):
        """Receive UDP packets."""
        log("UDP started (socket mode)")
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

        self.setup_socket(sock)

        while True:
            try:
                sock.bind((self.localaddr, self.myport))
            except socket.error:
                failed = 0
                if sys.exc_info()[1].args[0] == 48:
                    log("Port %s in use, trying next" % self.myport)
                    self.myport += 1
                    failed = 1
                if not failed: break
        log("Bound to %s" % self.myport)
 
        while True:
            # This is interrupted...?
            try:
                (data, addr) = sock.recvfrom(65535)
            except socket.error:
                if sys.exc_info()[1].args[0] != 4: # Interrupted system call
                    raise sys.exc_info()[1]     # not for us to catch 
                # Reinvoke it
                continue 
            self.handle_packet(data, addr)

    def server_udp_PCAP(self):
        log("UDP started (pcap mode)")
        if self.config["interface"] == "":
            import pcapy
            devs = pcapy.findalldevs()
            log("Network interfaces: %s" % devs)
            if len(devs) == 1:
                self.config["interface"] = devs[0]
                log("Automatically setting to %s" % self.config["interface"])
            else:
                log("*** Please set 'interface' in config.txt to one of ")
                log("*** the interfaces above and restart.")
                sys.exit(-5)
        import sumiserv
        sumiserv.cfg = {"interface": self.config["interface"]}
        def callback(data, pkt):
            #addr = ("0.0.0.0.","0.0.0.0") #?? TODO: find address from pkt IP header
            addr = (".".join(map(str, struct.unpack("!4B", pkt[14+12:14+16]))),
                   ".".join(map(str, struct.unpack("!4B", pkt[14+16:14+20]))))
            self.handle_packet(data, addr)

        def decode(pkt):
            return (sumiserv.get_udp_data(pkt), pkt)

        capture(decode, "udp", callback)

    def cli_user_input(self):
        """Client user input (not used anymore), belongs in separate program."""
        input_lock.acquire()   # wait for messaging program to be connected
        log("Started user input thread... you may now type")
        while True:
            line = sys.stdin.readline()
            if line == "":    # EOF, exit
                return
            line = line[:-1]  # Strip \n
            if line == "":    # blank line (\n), ignore
                continue
            args = line.split()
            if args[0] == "/get":
                try:
                    self.request(args)
                except IndexError:
                    log("Usage: sumiget <transport> <server_nick> <file>")
            else:
                log("Unrecognized command: %s" % line)
                #self.sendmsg(irc_chan, line)
        # DO SUMI SEC THEN ENCRYPT MSGS & PACKETS
        # Pre-auth. aes'd sumi send > irc_maxlen
        # MAX IRC PRIVMSG IN XCHAT: 452 in #sumi, 454 in #a (??) 462 in #a*31
        # ON NGIRCD: COMMAND_LEN - 1, is 513-1 is 512. Room for PRIVMSG+nick.
        # ":jeff PRIVMSG " = 14
        # Continuations are now implemented in some transports (segment()),
        # TODO: Also, think about combining some elements of sumi send with
        # sumi sec. Consider making sumi sec handle the auth packet, and
        # sumi send deal strictly with file transfers. This way, sumi sec+
        # sumi login can be implemented later; allowing remote admin.
        # TODO: Actually, why not simply extend sumi send to allow remote admn.

    def recvmsg_thread(self, u):
        """Thread to wait for messages from server.
        Passes messages to handle_server_message."""
        def callback(user_nick, msg):
            return self.handle_server_message(user_nick, msg)

        if not u.has_key("recvmsg"):
            log("%s is missing recvmsg transport" % u["nick"])
            log("recvmsg is necessary for crypto (shouldn't happen)")
            sys.exit(-2)
        u["recvmsg"](callback, server=False)
    
    def encrypt(self, u, msg):
        """Encrypt a message using u's key and IV."""
        e = encrypt_msg(msg, u["sesskey"], u["sessiv"])
        return e

    def decrypt(self, u, msg):
        """Decrypt a message using nick's key and IV."""
        return decrypt_msg(msg, u["sesskey"], u["sessiv"])

    def handle_server_message(self, nick, msg):
        """Handle a message received from the server on the transport.
        Used for crypto.
        
        Returns False if the message couldn't be processed, True if it 
        could. This is the callback for libsumi's capture() function, so
        note that returning a string will stop packet capturing."""

        if nick == "(transport_ready)":
            log("Releasing transport lock: %s" % msg)
            transports[msg].release()

        if not self.senders.has_key(nick):
            print "UNRECOGNIZED <%s> %s" % (nick, msg)
            print self.senders.keys()
            return False

        u = self.senders[nick]

        # First check for plain-text messages
        if msg.startswith("error: "):
            error_msg = msg[len("error: "):]
            log("*** Error: %s: %s" % (nick, error_msg))
            u["server_error"] = error_msg
            self.callback(u["nick"], "error", error_msg)
            return False

        print "MSG: %s" % msg

        # Always base64'd from here on
        try:
            raw = base64.decodestring(msg)
        except binascii.Error:
            log("%s couldn't decode?!" % msg)
            return False

        # Server will send three things: pubkeys, nonce1/2, nonce2/2
        # in two messages (pubkeys+nonce1/2, nonce2/2). We can tell which
        # message we are receiving by what we received previously.
        if not u.has_key("crypto_state") and u.has_key("sent_sec"):  
            # pubkeys+nonce1/2
            g = time.time()                                 #  -> req1/2
            u["got_nonce1"] = g
            d1 = g - u["sent_sec"]
            log("Took %s seconds to get pk+nonce1/2 after sumi sec" % d1)
            if round(d1) < INTERLOCK_DELAY:
                self.callback(u["nick"], "sec_fail1")
                log("INTERLOCK FAILURE 1! %s < %s" % (d1, INTERLOCK_DELAY))
                log("Possible attack. Not trusting the server. Aborting.")
                return False

            self.set_handshake_status(u, "Interlocking-1")
            log("Got pubkeys + nonce1/2")
            # First message...its pubkeys + nonce1/2
            skeys = unpack_keys(raw[0:32*3])
            log("skeys=%s" % skeys)

            if True:#self.config.get("crypt_active"):
                nonce_1 = raw[32*3:]
                # can't decrypt now, since only have half; keep it
                log("nonce_1=%s" % ([nonce_1,]))
                u["nonce_1"] = nonce_1

            # Find out shared/private keys (pkeys)
            ckeys = u["ckeys"]
            pkeys = []
            for ck, sk in zip(ckeys, skeys):
                pkeys.append(ck.DH_recv(sk))
            log("pkeys=%s" % pkeys)
            sesskey = hash128(pkeys[0]) + hash128(pkeys[1])
            sessiv = pkeys[2]
            u["sesskey"] = sesskey
            u["sessiv"] = sessiv

            clear_req = u["request_clear"]
            log("sesskey/iv: %s" % ([sesskey, sessiv],))
            enc_req = self.encrypt(u, clear_req)
            log("ENC REQ: %s" % ([enc_req],))
            u["request_enc"] = enc_req

            req1 = enc_req[0::2]   # even
            req2 = enc_req[1::2]   # odd 
            u["request_1"] = req1
            u["request_2"] = req2

            # Send 1/2 of encrypted sumi send request 
            u["sent_req1"] = time.time()
            self.sendmsg(u, b64(req1))
        
            u["crypto_state"] = 1

        elif u["crypto_state"] == 1:   # nonce2/2->req2/2
            g = time.time()
            u["got_nonce2"] = g
            d2 = g - u["sent_req1"]
            log("Took %s seconds to get nonce2" % d2)
            if round(d2) < INTERLOCK_DELAY:
                self.callback(u["nick"], "sec_fail2")
                log("INTERLOCK FAILURE 2! Possible attack, aborting.")
                log("%s < %s" % (d2, INTERLOCK_DELAY))
                return False

            log("Got nonce 2/2")
            self.set_handshake_status(u, "Interlocking-2")
            # Second message: nonce2/2j
            nonce_1 = u["nonce_1"]
            nonce_2 = raw
            nonce = self.decrypt(u, interleave(nonce_1, nonce_2))
            print "NONCE=%s" % ([nonce,])
            u["nonce"] = nonce
            u["nonce_hash"] = hash160(nonce)

            # Send 2/2 of encrypted sumi send request. Expect response soon.
            self.sendmsg(u, b64(u["request_2"]))
            u["sent_req2"] = time.time()

            u["crypto_state"] = 2

        return True

    def set_handshake_status(self, u, status):
        """Set handshake status to status, and send a callback message
        updating it with the new status and existing countdown."""
        u["handshake_status"] = status
        self.callback(u["nick"], "req_count",
            u["handshake_count"],
            u["handshake_status"])

    def setup_recvmsg(self, u):
        """Setup the receiving message thread."""
        # Lock released when connected
        transports[u["transport"]] = thread.allocate_lock()
        transports[u["transport"]].acquire()
        thread.start_new_thread(self.wrap_thread, 
                (self.recvmsg_thread, (u, )))

        # Use this line to wait until recvmsg is connected:
        #transports[u["transport"]].acquire()

    def setup_transport_crypto(self, u):
        """Send sumi sec (secure) command, setting up an encrypted channel."""
        log("Setting up cryptography...")

        self.set_handshake_status(u, "Key exchange")

        # All crypto library imports are inside functions, rather than at
        # the top of the file, so that we can run without them if needed.
        from ecc.ecc import ecc

        # Generate our keys
        ckeys = []
        for i in range(3):
            log("Generating key #%s" % i)
            # XXX: ECC key generation crashes on amd64
            ckeys.append(ecc(ord(random_bytes(1)) + 1))
       
        # Send our public keys to server
        raw = ""
        for k in ckeys:
            raw += "".join(k.publicKey())

        log("Our keys: %s" % b64(raw))
        u["ckeys"] = ckeys
        
        # Wait for server's public keys. Start a receiving thread here
        # because setting up crypto is the only time we receive transport
        # messages from the server.  N.B.: if recvmsg() is called, it must
        # be called BEFORE the first sendmsg() (see modirclib.py).
        #thread.start_new_thread(self.recvmsg_thread, (u["nick"], ))

        # Wait until connected before sending sendmsg!
        log("Waiting for recvmsg_thread...")
        transports[u["transport"]].acquire()

        u["sent_sec"] = time.time()
        self.sendmsg(u, "sumi sec %s" % b64(raw))

        log(">>>>> %s" % u["nick"])

    def wrap_thread(self, f, args):
        try:
            f(*args)
        except None:# Exception, x:
            print "(thread) Exception: %s at %s" % (x,
                    sys.exc_info()[2].tb_lineno)
            raise x

    def validate_config(self):
        """Validate configuration after loading it, possibly modifying it
           by filling in defaults.

           Return None if configurationi s valid, or an error message if
           not."""

        # Errors
        r = []

        if self.config.has_key("myip"):
            if self.config["myip"] != "":
                self.myip = self.config["myip"]
                try:
                    self.myip = socket.gethostbyname(self.myip)
                except:
                    log("Couldn't resolve %s" % self.myip)
                    sys.exit(3)
                log("Resolved hostname to: %s" % self.myip)
            else:
                self.myip = get_default_ip()
                log("Using IP: %s" % self.myip)
        else:
            log("IP not specified, getting network interface list...")
            log("\nSelect an interface, or set 'myip' in config.txt for auto.")

            (self.config["interface"], self.config["myip"], ignore,
                self.config["mtu"]) = select_if()

            log("Saving settings. Please review them in config.txt, edit "
                    "as necessary, and restart.")
            self.on_exit()

        if self.config.has_key("myport"):
            self.myport = self.config["myport"]
        else:
            log("defaulting to port 41170")
            self.myport = 41170

        # Your local IP to bind to. This can be your private IP if you're behind
        # a NAT it can be the same as "myip", or it can be "" meaning bind to
        # all ifaces
        self.localaddr = ""

        # Not used in all transports
        if self.config.has_key("irc_nick"):
            self.irc_nick = self.config["irc_nick"]
        else:
            log("No IRC nick specified. What to use? ")
            self.irc_nick = sys.stdin.readline()[:-1]
       
        if self.config.has_key("irc_name"):
            self.irc_name = self.config["irc_name"]
        else:
            self.irc_name = self.irc_nick

        if self.config.has_key("mtu"):
            self.mss = mtu2mss(self.config["mtu"], 
                    self.config["data_chan_type"])
            log("Maximum file size (over UDP): %sB" % human_readable_size(
                (self.mss - IPHDRSZ - UDPHDRSZ - SUMIHDRSZ) * 0xffffffffL))
            log("Using MSS = %s" % self.mss)

        else:
            try:
                self.mss
            except:
                r.append("MSS was not set, please set it in the Client tab.")

        if self.config.get("crypt"):
            random_init()

        if self.config.has_key("rwinsz"):
            self.rwinsz = self.config["rwinsz"]
            self.rwinsz_old = 0
                                  #RWINSZ never changes here! TODO:if it does,
                                  # then rwinsz_old MUST be updated to reflect.
        else:
            r.append("Please set rwinsz. Thank you.")

        if self.config.has_key("bandwidth"):
            self.bandwidth = self.config["bandwidth"]
        else:
            r.append("Please set your bandwidth.")
            sys.exit(5)

        # More validation, prompted by SJ
        if not self.config.get("allow_local") and \
           is_nonroutable_ip(self.myip):
               r.append("""Your IP address, %s (%s) is nonroutable.
Please choose an Internet-accessible IP address. If you are not sure what 
your IP is, go to http://whatismyip.com/. Your IP can be set in the Client 
tab of sumigetw.""" % (self.myip, self.config["myip"]))

        # Force trailing slash?
        #if self.config["dl_dir"][:1] != "/" and \
        #   self.config["dl_dir"][:1] != "\\": 
        #   self.config["dl_dir"] += "/"
        if len(self.config.get("dl_dir", "")) == 0 or not \
                os.access(self.config["dl_dir"], os.W_OK | os.X_OK | os.R_OK):
            new_dir = base_path + "incoming"
            log("Note: dl_dir %s inaccessible, trying %s instead" %
                    (self.config["dl_dir"], new_dir))
           
            if not os.access(new_dir, os.W_OK | os.X_OK | os.R_OK):
                os.mkdir(new_dir)
            
            if not os.access(new_dir, os.W_OK | os.X_OK | os.R_OK):
                r.append("""An invalid download directory, %s, was specified.
Tried to use a valid directory of %s but it couldn't be accessed.""")

            self.config["dl_dir"] = new_dir

        if r == []:
            return None     # Passed all tests
        else:
            return "\n\n".join(r)

    def sendmsg(self, u, msg):
        """Send a message over the covert channel using the loaded
        transport module."""
        log(">>%s>%s" % (u["nick"], msg))
        return u["sendmsg"](u["nick"], msg)

    def abort(self, u):
        """Abort transfer to user."""

        if u.get("aborted"):         # can only abort once
            return False

        if u.get("handshake_error"): # never got pass handshake
            return False

        self.sendmsg(u, "!")

        # Keep around user, because server won't abort transfer immediately.
        # Transport takes some time, and in that time we'll still be receiving
        # packets; need to recognize them to ignore them. (Presumably, better
        # to ignore them than keep them--if user wanted to abort, probably
        # wants to abort immediately.) Keep prefix so can recognize.
        prefix = u.get("prefix")
        #self.clear_server(u)
        u["prefix"] = prefix
        u["aborted"] = True

        self.callback(u["nick"], "aborted")

        return True

    def make_request(self, u, file):
        """Build a message to request file and generate a random prefix."""

        assert isinstance(file, basestring), "%s isn't a string" % file

        prefix = random_bytes(3)
        u["prefix"] = prefix

        if u["crypt_data"]:
            # Choose the 256-bit symmetric key and 128-bit IV, which will be
            # sent over the transport channel. The transport channel should be
            # secure. TODO: mark transports secure (Tor), crypt_req if not.
            data_key = random_bytes(32)
            data_iv = random_bytes(16)
            u["data_key"] = data_key
            u["data_iv"] = unpack_num(data_iv)
            def ctr_proc():
                u["ctr"] += 1
                x = pack_num(u["ctr"] % 2**128)
                x = "\0" * (16 - len(x)) + x
                assert len(x) == 16, "ctr_proc len %s not 16" % len(x)
                return x
            u["crypto_obj"] = get_cipher().new(
                    data_key, get_cipher().MODE_CTR, counter=ctr_proc)
        else:
            data_key = data_iv = ""

        # If multicast_group is specified, use that
        if u.has_key("multicast_group"):
            ip = u["multicast_group"]
            if not is_multicast(ip):
                # An attacker could claim that an address is multicast, when
                # it is really an innocent victim. At most they'll get a
                # spurious auth packet but don't allow even that.
                fatal("Address %s is not a multicast address!" % ip)
            if not self.config["allow_multicast"]:
                fatal("Attempted to download file from multicast group %s" %
                        ip + ", but allow_multicast is not enabled.")
                
        else:
            ip = self.myip

        args = {"f":file,
            #"o":offset,  # offset moved to sumi auth (know file size)
            "i":ip, "n":self.myport, "m":self.mss,
            "p":b64(prefix),
            "b":self.bandwidth,
            "w":self.rwinsz, 
            "c":u["control_proto"],
            "x":b64(data_key + data_iv),
            "d":self.config["data_chan_type"]}

        if u["control_proto"] == "fec":
            u["redundancy"] = self.config.get("redundancy", 20.0)
            u["fec_k"] = redundancy_to_k(u["redundancy"])
            u["fec_group"] = {}
            args["r"] = u["redundancy"]

        msg = "sumi send " + pack_args(args)

        return msg

    def clear_server(self, u):
        """Clear information about a server, but save their nick."""

        self.save_lost(u)
        nick = u["nick"]
        print "Clearing %s..." % nick
        u.clear()
        u["nick"] = nick
        return u

    def setup_mcast(self, u):
        """Join a multicast group for u, if any. Should be called
        after make_request() since the multicast address validation
        is performed there."""
        if u.has_key("multicast_group"):
            for s in self.sockets:
                # TODO: callback
                self.mcast_op(s, socket.IP_ADD_MEMBERSHIP, u["multicast_group"])


    def request(self, args):
        """Request a file from a server. args are
        (.sumi filename) or (transport, nick, filename).

        Returns nothing. Callback if fail."""

        assert type(args) == types.ListType, \
                "request was passed %s, but isn't a list!" % str(args)
        if len(args) == 1:
            fn = args[0]
            log("Loading from .sumi %s" % fn)
            u = self.load_transfer(fn)
            args = u["transport"], u["nick"], u["filename"]
            if not args:
                log("Warning: %s could not be loaded" % fn)
                # can't callback because don't necessarily have nick
                #self.callback(nick, "bad_file", fn)
                return
        else:
            u = {}      # create

        assert len(args) == 3, \
                "Needed transport,nick,filename but got: %s" % str(args)

        transport, nick, filename = args

        self.callback(nick, "new_xfer", *args)

        # command line args are now the sole form of user input;
        self.callback(nick, "t_wait")   # transport waiting, see below

        if self.senders.has_key(nick) and not self.senders[nick].get("aborted"):
            # TODO: Index senders based on unique key..instead of nick
            # Then we could have multiple transfers from same user, same time!
            log("Already have an in-progress transfer from %s" % nick)
            log(self.senders)
            self.callback(nick, "1xferonly")
            #print "Senders: ", self.senders
            return

        self.senders[nick] = u    # initialize
        u = self.senders[nick]
        u["nick"] = nick

        # Setup transport system
        u["transport"] = transport
        if not self.load_transport(transport, u):
            # Callback within load_transport
            return
        u["filename"] = filename

        u["handshake_count"] = 0
        u["handshake_status"] = "Handshaking"
        u["control_proto"] = self.config["control_protocol"]

        # If transport can receive messages, set it up. Useful to receive
        # error messages back from server.
        if "recvmsg" in u:
            self.setup_recvmsg(u)
        else:
            log("No recvmsg for this transport, server feedback not available")

        if u.get("crypt_req", False):
            if not "recvmsg" in u:
                log("Sorry, this transport lacks a recvmsg, so "
                        "transport encryption is not available.")
                self.callback(nick, "t_no_recvmsg", transport) 
                return
            # Store request since its sent in halves
            u["request_clear"] = self.make_request(u, filename)
            self.setup_mcast(u)
            self.setup_transport_crypto(u)
        else:
            # Even if crypt is enabled, with a secure transport don't encrypt
            # the request.
            msg = self.make_request(u, filename)
            self.setup_mcast(u)
            self.sendmsg(u, msg) 

        log("Sent")
        self.callback(nick, "req_sent") # request sent (handshaking)

        # Countdown. This provides a timeout for handshaking with nonexistant
        # senders, so the user isn't left hanging.
        maxwait = self.config["maxwait"]

        if u.get("crypt_req", False):
            # Factor in time to interlock (2*T, plus another T for safety)
            maxwait += 3 * INTERLOCK_DELAY

        for x in range(maxwait, 0, -1):
            # If received fn in this time, then exists, so stop countdown
            #if not self.senders.has_key(u["nick"]):
            #        return False    # some other error
            if u.has_key("got_first"):
                return    # Success: don't break - otherwise will timeout.
            if u.has_key("handshake_error"):
                #self.clear_server(u)
                u["handshake_error"] = True
                return    # Error set by callback already, get out
            if u.has_key("server_error"):
                return
            if not u.has_key("handshake_status"):
                # user was deleted, probably finished
                return
            if u.get("bytes", -1) >= u.get("disk_size", 0):
                # file is complete (don't finish if no bytes or size)
                self.finish_xfer(u)
                #self.callback(u["nick"], "xfer_fin", 0, 0, 0, [])
                return
            u["handshake_count"] = x 
            self.callback(u["nick"], "req_count", x, u["handshake_status"])
            time.sleep(1)

        self.callback(u["nick"], "timeout")
        print "TIMED OUT SO CLEARING"
        self.clear_server(u)
        self.senders.pop(u["nick"])
        return 

    def set_callback(self, f):
        """Set callback to be used for handling notifications."""
        self.callback = f

    def default_cb(self, cmd, *args):
        """Default callback: prints received command."""
        log("(CB)%s: %s" % (cmd, ",".join(list(map(str, args)))))

    def load_transport(self, transport, u):
        """Load transport/mod<transport> for user, and initialize if not
        already initialized.
 
        Returns whether succeeds."""

        global input_lock, sendmsg, transport_init, transports
        # Import the transport. This may fail, if, for example, there is
        # no such transport module.
        try:
            sys.path.insert(0, os.path.dirname(sys.argv[0]))
            t = __import__("transport.mod" + transport, None, None,
                           ["transport_init", "sendmsg"])
        except ImportError, e:
            # Anytime a transfer fails, or isn't in progress, should pop it
            # So more transfers can come from the same users.
            self.clear_server(u)
            self.senders.pop(u["nick"])
            self.callback(u["nick"], "t_import_fail", e)
            return False

        t.segment = segment
        t.cfg = self.config
        t.u = u
        t.log = log
        t.capture = capture
        t.get_tcp_data = get_tcp_data
    
        print "t=",t

        u["sendmsg"] = t.sendmsg
        u["transport_init"] = t.transport_init
        
        # If crypt enabled, and insecure transport, encrypt transport
        u["crypt_data"] = self.config["crypt"]
        u["crypt_req"] = self.config["crypt"] and not t.is_secure() 
       
        log("crypt_data=%s, crypt_req=%s" % (u["crypt_data"], u["crypt_req"]))

        # Initialize if not
        if transports.get(transport):
            pass    # already initialized
            log("Not initing %s" % transport)
        else:
            u["transport_init"]()
            transports[transport] = 1   # Initialize only once
            log("Just inited %s" % transport)

        if hasattr(t, "recvmsg"):
            # If can't receive messages, crypto not available
            u["recvmsg"] = t.recvmsg
       
        # Initialize user if possible
        if hasattr(t, "user_init"):
            u["user_init"] = t.user_init
            self.callback(u["nick"], "t_user")
            log("Initializing user...")
            try:
                u["user_init"](u["nick"])
            except Exception, e:
                log("user_init(%s) failed: %s" % (u["nick"], e))
                self.callback(u["nick"], "t_user_fail", e)
                return False

        return True

    def main(self, args):
        """Text-mode client. There isn't much user-friendliness here--the
        callbacks simply dump the passed arguments to stdout. There used to
        be an interactive interface, cli_user_input, but it is no longer
        supported.

        args: arguments, without argv[0]
        
        In the future, this interface should be more usable."""

        e = self.validate_config()
        if e:
            log("Configuration error")
            log(e)
            raise SystemExit

        thread.start_new_thread(self.wrap_thread, (self.thread_nak_timer, ()))
        thread.start_new_thread(self.wrap_thread, (self.thread_ack_timer, ()))

        thread.start_new_thread(self.wrap_thread, (self.request, (args, )))

        # This thread will release() input_lock, letting thread_request to go
        #transport_init()

        input_lock.acquire()
        log("RELEASED")
        input_lock.release()

        # Main thread is UDP server. There is no transport thread, its sendmsg
        self.recv_packets()   # start waiting before requesting

    def on_exit(self): 
        """Called by GUI upon exiting."""
        log("Cleaning up...")

        try:
            savefile = file(config_file, "w")

            savefile.write("""# Client configuration file
# Saved by $Id$
# Please note - ALL COMMENTS IN THIS FILE WILL BE DESTROYED\n""")
            # ^ I place all comments in config.txt.default instead, or the docs
            pprint.pprint(self.config, savefile)
            savefile.close()
        except IOError, e:
            log("!! Couldn't save configuration file: %s" % e)

        self.set_callback(lambda *x: 0)

        # Abort all the transfers, be polite. Rudely leaving without aborting
        # will cause the server to time out after not receiving our acks, but
        # it takes a while to time out and wastes bandwidth.
        if hasattr(self, "senders"):
            for x in self.senders.keys():
                log("Aborting %s" % x)
                self.abort(self.senders[x])

        log("Exiting now")
        raise SystemExit
        #sys.exit()
        #os._exit()

def on_sigusr1(signo, intsf):
    """SIGUSR1 is used on Unix for signalling multiple transfers."""
    global base_path
    log("Got SIGUSR1 (%s %s) calling" % (signo, intsf))
    (transport, nick, filename) = file(base_path + "run", 
        "rb").readline().split("\t")
    log("-> %s %s %s " % (transport,nick,filename))
    #Client().main(transport, nick, filename)
    # TODO: it needs to be possible to run multiple xfers per program, fix it
    #client.main(transport, nick, filename) # Runs servers twice (init_t + udp)
    #client.request(nick, filename)  # Interrupted system call recvfrom(65535)
    #sys.exit(0)

# CLI uses this on-exit
def on_exit(signo=0, intsf=0):
    log("Cleaning up...(signal %s %s)" % (signo, intsf))
    #os.unlink(base_path + "sumiget.pid") 

def pre_main(invoke_req_handler):
    """Before creating the client, this function handles multiple instances."""
    global client

    # Multi-client support
    if sys.platform == 'win32':
        # win32gui.PumpWaitingMessages()?   # will be handled by wxWindows
        pass 
    else:
        signal.signal(signal.SIGUSR1, invoke_req_handler)    # set handler
    # Called by GUI on exit instead
    signal.signal(signal.SIGINT, on_exit)

    # 0=seperate program per transfer, 1=all in one (current)
    # =1 works in Unix using signals, but have to resize the frame.
    multiple_instances = 1

    if multiple_instances and os.access(base_path + "sumiget.pid", os.F_OK):
        # TODO: file locking so will be unlocked if crashes
        # PID file exists, program (should) be running
        # So signal it, pass control onto - it does work, not us
        master = file(base_path+"sumiget.pid","rb").read()
        if len(master) != 0:   # If empty, be master
            file(base_path + "run", "wb").write("\t".join(sys.argv[1:]))
            my_master = int(master)
            log("Passing to: %s" % my_master)
            failed = 0 
            try:
                if sys.platform == 'win32':
                    import win32gui
                    import win32con
                    try:
                        win32gui.BringWindowToTop(my_master)
                    except: 
                        failed = 1
                    else:
                        win32gui.SendMessage(my_master, win32con.WM_SIZE, 0, 0)
                else:
                    os.kill(my_master, signal.SIGUSR1) 
            except OSError:
                failed = 1
            if not failed: sys.exit(0)   # Otherwise, will be master
            log("Failed to pass to %s" % my_master)
            #sys.exit(-1)
        # File locking would be good; if locked then write cmdline, os.kill other
    # Moved to GUI
    #pidf = file(base_path + "sumiget.pid", "wb")
    #pidf.write(str(os.getpid()))
    #pidf.close() 

def doctest():
    import doctest

    # Available to all doctests for convenience
    global c
    c = Client()

    log("sumiget running doctests...")

    failures, tests = doctest.testmod()
    sys.exit(failures != 0)


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "-t":
        # sumiget's only command line flag: -t is self-test and exit
        # (can also pass -t -v for verbose, handled by doctest module)
        doctest()

    pre_main(on_sigusr1)

    if len(sys.argv) == 4:
        log("Getting <%s> from <%s> using <%s>..." % tuple(sys.argv[1:]))

    try:
        client = Client()
        client.main(sys.argv[1:])
    except (KeyboardInterrupt, SystemExit):
        on_exit(None, None)
