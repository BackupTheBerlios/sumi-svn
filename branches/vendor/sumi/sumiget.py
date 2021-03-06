#!/usr/bin/env python
# Created:20040117M
# By Jeff Connelly

# SUMI downloader, invoke: sumiget.py get <server> <file>
# See also: sumigetw.py

import thread
import base64
import random
import socket
import string
import struct
import sys
import os
import md5
import time
import popen2
from libsumi import *

from getifaces import get_ifaces, get_default_ip

input_lock = thread.allocate_lock()
#transport = "python -u transport/sumi-irc.py"
#global transport   
#transport = "mirc"

base_path = os.path.abspath(os.path.dirname(sys.argv[0])) + os.sep
config_file = base_path + "config.py"
print "Using config file: ", config_file

# Setup run-time path for loading transports
sys.path.append(os.path.realpath(os.path.dirname(sys.argv[0])))
sys.path.append(os.path.realpath(os.path.dirname(sys.argv[0])) + "/../")
#import transport.modmirc

global transports
transports = {}

# Make stdout unbuffered. Not sure if this is still needed.
real_stdout = sys.stdout
class unbuffered_stdout:
    def write(self, s):
        real_stdout.write(s)
        # Check to see if hash flush attribute, because the Blcakhole object
        # (used when ran as a GUI app with no console) does not
        if (hasattr(real_stdout, "flush")): real_stdout.flush()
sys.stdout = unbuffered_stdout()

# Used by transports to segment messages
def segment(nick, msg, max, callback):
    n = 0
    # Length of each segment, to overcome limitations of IRC privmsg's, the
    # msg will be split into segments within range of privmsg limit. The
    # last segment has no ">" prefix, other segments do.
    #MAX_LEN = 550#3#512 - len(":PRIVMSG") - len(nick)
    prefix = ">"
    #print "I'm segmenting **%s**" % (msg) 
    while len(msg[n:n+max]):
        if n + max >= len(msg):
            prefix = ""
        #print ">", nick, prefix + msg[n:n+max]
        callback(nick, prefix + msg[n:n+max])
        #sendmsg_1(prefix + msg[n:n+max])
        #print(prefix + msg[n:n+MAX_LEN])
        n += max


class Client:

    def __init__(self):
        print "Loading config...",
        # Mode "U" for universal newlines, so \r\n is okay
        self.config = eval("".join(open(config_file, "rU").read()))
        print "OK"

        if self.config.has_key("myip"):
            if self.config["myip"] != "":
                self.myip = self.config["myip"]
            else:
                self.myip = get_default_ip()
                print "Using IP: ", self.myip
        else:
            print "IP not specified, getting network interface list..."

            # Look for an up interface. Use get_ifaces instead of just the
            # IP so we can also get the netmask, too
            ifaces = get_ifaces()
            for name in ifaces:
                if not ifaces[name].has_key("status") or \
                   ifaces[name]["status"] != True and \
                   ifaces[name]["status"] != "active":
                    continue
                if not ifaces[name]["inet"]: continue

                print name,ifaces[name]["inet"],ifaces[name]["netmask"]
            # XXX: This is a problem for wxfer.py! It needs input! This is
            # how it should be, though. Chuck popen into the trash.
            print "Which interface? ",
            i = sys.stdin.readline()[:-1]
            print "Using IP ", ifaces[i]["inet"]
            self.myip = ifaces[i]["inet"]
            self.netmask = ifaces[i]["netmask"]   # save for later
            self.netmask = int(ifaces[i]["mtu"]) - 28

        if self.config.has_key("myport"):
            self.myport = self.config["myport"]
        else:
            print "defaulting to port 41170"
            self.myport = 41170

        # Your local IP to bind to. This can be your private IP if you're behind
        # a NAT it can be the same as "myip", or it can be "" meaning bind to
        # all ifaces
        self.localaddr = ""

        self.senders = {}

        if self.config.has_key("irc_nick"):
            self.irc_nick = self.config["irc_nick"]
        else:
            print "No IRC nick specified. What to use? "
            self.irc_nick = sys.stdin.readline()[:-1]
       
        if self.config.has_key("irc_name"):
            self.irc_name = self.config["irc_name"]
        else:
            self.irc_name = self.irc_nick

        if self.config.has_key("mss"):
            self.mss = self.config["mss"]
        else:
            try:
                self.mss
            except:
                print "MSS not found! Please set it."
                raise SystemExit        
   
        if self.config.has_key("rwinsz"):
            self.rwinsz = self.config["rwinsz"]
            self.rwinsz_old = 0
                                  #RWINSZ never changes here! TODO:if it does,
                                  # then rwinsz_old MUST be updated to reflect.
        else:
            print "Please set rwinsz"
            raise SystemExit 

        if self.config.has_key("bandwidth"):
            self.bandwidth = self.config["bandwidth"]
        else:
            print "Please set bandwidth"
            sys.exit(5)

        self.set_callback(self.default_cb)   # override me please

    def on_msg(self, nick, msg):
        """Handle incoming messages."""
        print "<%s> %s" % (nick, msg)
        # This isn't used anymore - its in the auth packet instead
        if (msg.find("sumi start ") == 0):
            args = unpack_args(msg[len("sumi start "):])
            (filename, offset, size) = (args["f"], args["o"], args["l"])

            offset = int(offset)
            size = int(size)

            if (self.senders.has_key(nick)):
                self.sendmsg(nick, "n%d" % self.rwinsz)
                self.rwinsz_old = self.rwinsz
                print "Starting n%d %s for %s,%d,%d" % (self.rwinsz, nick,
                    filename, offset, size)
            else:
                print "ERROR: user not known, stranger trying to sumi start!"
                print "Senders: ", self.senders
        elif (msg.find("error: ") == 0):
            errmsg = msg[len("error: "):]
            print "*** Error: ", errmsg

    # Write the resume file for transfer from nick
    def save_lost(self, x):
        lost = ",".join(map(str, self.senders[x]["lost"].keys()))
        lost += "," + str(self.senders[x]["at"])   # last is at, cur/last
        if (lost != ""):
            self.senders[x]["fs"].seek(0)
            self.senders[x]["fs"].truncate()
            self.senders[x]["fs"].flush()
            self.senders[x]["fs"].write(lost)
            self.senders[x]["fs"].flush()
            #print "WROTE LOST: ",lost

    def handle_packet(self, data, addr):
        """Handle received packets."""
        prefix  = data[:3]
        (seqno, ) = struct.unpack("!L", "\0" + data[3:6])
 
        # Find nick that is associated with the random prefix; which is the
        # only way to identify the source.
        # The data structures aren't setup very efficiently
        nick = None
        for x in self.senders:
            if self.senders[x].has_key("prefix") and \
               self.senders[x]["prefix"] == prefix:
                nick = x
                break
        if nick == None:
            print "DATA:UNKNOWN PREFIX!",len(data),"bytes from",addr
            return
        #print "Incoming:",len(data),"bytes from",addr,"=",nick," #",seqno

        self.senders[nick]["retries"] = 0   # acks worked
 
        self.senders[nick]["last_msg"] = time.time()

        # XXX: Resuming sometimes asks for packets more than once, fix it.
        #     Maybe the dupe could be compared to see if any errors?
        # Last most recently received packet, for resuming
        self.senders[nick]["at"] = seqno 
        # Sequence number is 3 bytes in the SUMI header in network order
        # (so a null can easily be prepended for conversion to a long),
        # this used to be partially stored in the source port, but PAT--
        # Port Address Translation--closely related to NAT, can mangle 
        # the srcport
        if (seqno == 0):       # all 0's = auth packet
            context = md5.md5()
            context.update(data)       # auth "key" is all of data, hash it
            #context.update("hi")#bad hash
            #hash = context.hexdigest() 
            hash = base64.encodestring(context.digest())[:-1]
            print "PKT:Got auth packet from",addr," for ",nick,
  
            print "PKT:Verifying prefix (authenticity of server)..."

            # XXX: Does this ever fail?
            if (data[0:3] == prefix and len(data) > len(prefix)):
                print "OK\n"
            else:
                print "failed!"

            # file metadata should be here (file length)
            (self.senders[nick]["size"], ) = struct.unpack("!L", 
                data[SUMIHDRSZ:SUMIHDRSZ + SUMIAUTHHDRSZ])
            print "SIZE:%d" % (self.senders[nick]["size"],)
            filename=data[SUMIHDRSZ+4:data[SUMIHDRSZ+4:].find("\0")+SUMIHDRSZ+4]

            self.senders[nick]["fn"] = filename
            print "Filename: <%s>" % filename   
            self.callback(nick, "info", self.senders[nick]["size"], \
                base64.encodestring(prefix)[:-1], filename, \
                self.senders[nick]["transport"], \
                self.config["data_chan_type"])

            if (self.mss != len(data)):
                print "WARNING: Downgrading MSS %d->%d, maybe set it lower?" % (self.mss, len(data))
                self.mss = len(data)
                if (self.mss < 256):
                    print "MSS is extremely low (%d), quitting" % self.mss
                    sys.exit(-1)

            # Open the file and set it up
            if not self.senders[nick].has_key("fh"):  #  file not open yet
                print "Opening tempout for %s..." % nick,
                self.senders[nick]["start"] = time.time()

                # These try/except blocks try to open the file rb+, but if
                # it fails with 'no such file', create them with wb+ and
                # open with rb+. Good candidate for a function!
                try:
                    self.senders[nick]["fh"] = open(self.config["dl_dir"] + \
                         self.senders[nick]["fn"], "rb+")  
                except IOError:
                    open(self.config["dl_dir"] + \
                        self.senders[nick]["fn"], "wb+").close()
                    self.senders[nick]["fh"] = open(self.config["dl_dir"] + \
                        self.senders[nick]["fn"], "rb+")
                print "open"

                # Open a new resuming file (create if needed)
                try:
                    self.senders[nick]["fs"] = open(self.config["dl_dir"] + \
                        self.senders[nick]["fn"] + ".lost", "rb+")   
                    is_resuming = 1  # unless proven otherwise
                except IOError:
                    open(self.config["dl_dir"] + \
                        self.senders[nick]["fn"] + ".lost", "wb+").close()
                    self.senders[nick]["fs"] = open(self.config["dl_dir"] + \
                        self.senders[nick]["fn"] + ".lost", "rb+")
                    is_resuming = 0   # empty resume file

                # Check if the data file exists, and if so, resume off it
                lostdata = None

                if (os.access(self.config["dl_dir"] + \
                    self.senders[nick]["fn"], os.R_OK)):
                   # The data file is readable, read lost data 
                    lostdata = self.senders[nick]["fs"].read().split(",")

                    # If the below is true, the xfer is complete--",X".
                    # TODO: Let user overwrite or cancel. Needs to have
                    # a better interface for this, GUI-able.
                    if (len(lostdata) == 2 and lostdata[0] == "" \
                        and int(lostdata[1]) == lostdata[1]):
                        is_resuming = 0
                        print self.senders[nick]["fn"] + " is finished!" + \
                              " It will not be resumed."
                        sys.exit(1)
                else: 
                    is_resuming = 0
                if (len(lostdata) <= 1): is_resuming = 0
                print "LEN LOSTDATA=",len(lostdata)

                is_resuming=0#FORCE
 
                # Setup lost
                if (is_resuming):   # this works
                    self.senders[nick]["at"] = int(lostdata.pop())
                    if (len(lostdata) == 0 or len(lostdata[0]) == 0):
                        # Several ways to do this. a) Go through the normal
                        #  process, and finish there. con: auth. pro: simple?
                        #  b) save user the time, finished. c) overwrite choice
                        print "File complete, not resumed"

                        # Right now, it finishes the transfer.
                        self.callback(nick, "fin", 0, \
                            self.senders[nick]["size"], \
                            0, "")

                        # TODO: give choice
                        #self.cb("overwrite?", self.senders[nick]["fn"]
                        #sys.exit(1)
                        return #XYZ
                    print "RESUMING"
                    print "IS_RESUMING: LOST: ",lostdata
                    self.senders[nick]["lost"] = {}
                    for x in lostdata:
                        try:
                            self.senders[nick]["lost"][int(x)] = 1
                        except ValueError:
                            pass    # don't add non-ints
                    print "LOADED LOSTS:",self.senders[nick]["lost"]

                    # Initialize the rwin with empty hashes, mark off missings
                    self.senders[nick]["rwin"] = {}
                    for x in range(1, self.senders[nick]["at"]):
                        self.senders[nick]["rwin"][int(x)] = 1   # received 
                    for L in self.senders[nick]["rwin"]:    # mark losses
                        self.senders[nick]["rwin"][int(L)] = 0

                    # Bytes received = (MSS * at) - (MSS * numlost)
# TODO: Fix error in reporting number of bytes received, off by about 16,
# even though it is saved correctly...this leads to >100% progress rates
                    # XXX: MSS's may be inconsistant across users! Corruption
                    self.senders[nick]["bytes"] = \
                        (self.mss * self.senders[nick]["at"]) - \
                        (self.mss * len(self.senders[nick]["lost"].keys()))

                    # Files don't store statistics like these
                    self.senders[nick]["all_lost"] = []  # blah
                    self.senders[nick]["rexmits"] = 0
                else:
                    # Initialize
                    self.senders[nick]["at"] = 0
                    self.senders[nick]["rexmits"] = 0
                    self.senders[nick]["all_lost"] = []
                    self.senders[nick]["bytes"] = 0  # bytes received
                    self.senders[nick]["lost"] = {}    # use: keys(), pop()..
    # RWIN is a list of all the packets, and if they occured (0=no),
    # incremented each time a packet of that seqno is received. Since
    # Python arrays don't automatically grow with assignment, a hash
    # is used instead. If "rwin" was an array, [], missed packets would
    # cause an IndexError. See
    #http://mail.python.org/pipermail/python-list/2003-May/165484.html
                # for rationale and some other class implementations
                    self.senders[nick]["rwin"] = {}


            # Tell the sender to start sending, we're ok
            print "Sending sumi auth"
            self.sendmsg(nick, "sumi auth " + pack_args({"m":self.mss,
                "s":addr[0], "h":hash, "o":self.senders[nick]["at"]}))
            # The rest of this function handles data transfer
            return
        else:
            # Prefix has been checked, seqno calculated, so just get to the data
            data = data[SUMIHDRSZ:]

            # All file data is received here
  
            self.senders[nick]["last_msg"] = time.time()
   
            offset = (seqno - 1) * (self.mss - SUMIHDRSZ)
            self.senders[nick]["fh"].seek(offset)
            self.senders[nick]["fh"].write(data)
            #sys.stdout.write(".");
            #print "WRITE:%d:%d:%d:%s"%(offset, offset + len(data), len(data))
            #self.callback(nick, "write", offset, offset + len(data), len(data), \
                #self.senders[nick]["size"], addr)
 
            self.senders[nick]["bytes"] += len(data)   # correct

            self.callback(nick, "write", offset, self.senders[nick]["bytes"],
                self.senders[nick]["size"], addr)

            # Mark down each packet in our receive window
            try:
                self.senders[nick]["rwin"][seqno] += 1
            except KeyError:
                self.senders[nick]["rwin"][seqno] = 1    # create
 
            if (self.senders[nick]["rwin"][seqno] >= 2):
                print "(DUPLICATE PACKET %d, IGNORED)" % seqno
                return
 
            #Check previous packets, see if they were lost (unless first packet)
            if (seqno > 1):
                i = 1 
                while 1:
                    if (not self.senders[nick]["rwin"].has_key(seqno - i)):
                        self.senders[nick]["lost"][seqno - i] = 1
                        self.senders[nick]["all_lost"].append(str(seqno - i))
                        i += 1
                    else:
                        break  # this one wasn't lost, so already checked
    
                if self.senders[nick]["lost"].has_key(seqno):
                    self.senders[nick]["lost"].pop(seqno)
                    print "Recovered packet ", seqno, self.senders[nick]["lost"]
                    self.senders[nick]["rexmits"] += 1
                    print "(rexmits = ", self.senders[nick]["rexmits"]
                    self.callback(nick, "rexmits", self.senders[nick]["rexmits"])
                    #on_timer()   # Maybe its all we need

            if (self.senders.has_key(nick) and len(self.senders[nick]["lost"])):
                self.callback(nick, "lost", self.senders[nick]["lost"].keys())
                #print "These packets are currently lost: ", self.senders[nick]["lost"].keys()
                self.save_lost(nick)
            else:
                self.callback(nick, "lost", ())

        # Less than full sized packet = last
        if (len(data) != self.mss - SUMIHDRSZ):
            print "NON-FULLSIZED: %d != %d" % (len(data), self.mss - SUMIHDRSZ)
            self.senders[nick]["gotlast"] = 1
            # File size is now sent in auth packet so no need to calc it here
            #self.senders[nick]["size"] = self.senders[nick]["fh"].tell()
            self.on_timer()     # have it check if finished

    def thread_timer(self):
        """Every RWINSZ seconds, send a nak of missing pkts up to that point."""
        while 1:
            time.sleep(self.rwinsz)
            self.on_timer()

    def on_timer(self):
        """Acknowledge to all senders and update bytes/second."""
        tmp_senders = self.senders.copy()
        for x in tmp_senders:
            if (not self.senders[x].has_key("lost")):  # not xfering yet
                self.senders[x]["retries"] = 0   # initialize
                continue

            # Worth trying to update bps here
            if self.senders[x].has_key("bytes"):
                if self.senders[x].has_key("last_bytes"):
                    bytes_per_rwinsz = \
                        self.senders[x]["bytes"] - self.senders[x]["last_bytes"]
          
                    rate = float(bytes_per_rwinsz) / float(self.rwinsz) 
                    # rate = delta_bytes / delta_time   (bytes arrived)
                    # eta = delta_bytes * rate          (bytes not arrived)
                    if (rate != 0):
                        eta = (self.senders[x]["size"] - self.senders[x]["bytes"]) / rate
                    else:
                        eta = 0
                    # Callback gets raw bits/sec and seconds remaining
                    self.callback(x, "rate", rate, eta)
                    self.senders[x]["last_bytes"] = self.senders[x]["bytes"]
                else:
                    self.senders[x]["last_bytes"] = self.senders[x]["bytes"]

            if (len(self.senders[x]["lost"]) == 0 and 
                self.senders[x].has_key("gotlast")):
                return self.finish_xfer(x) # there's nothing left, we're done!
            try:
                # Some missing packets, finish it up
                lost = ",".join(map(str, self.senders[x]["lost"].keys()))
                # Compress by omitting redundant elements to ease bandwidth
                if (self.rwinsz_old == self.rwinsz and lost == ""):
                    self.sendmsg(x, "n")
                elif (lost == ""):
                    self.sendmsg(x, "n%d" % self.rwinsz)
                else:
                    self.sendmsg(x, ("n%d," % self.rwinsz) + lost)

                self.senders[x]["retries"] += 1
                if (self.senders[x]["retries"] > 5):
                    print x,"exceeded maximum retries (5), cancelling"
                    self.senders.pop(x)
                    self.callback(x, "timeout")

                self.rwinsz_old = self.rwinsz# TODO: update if changes..but need
                                          # to give the win on first(right)

            except KeyError:   # sender ceased existance
                pass

    def finish_xfer(self, nick):
        """Finish the file transfer."""

        # Nothing lost anymore, update. Saved as ",X" where X = last packet.
        print "DONE - UPDATING"
        self.save_lost(nick)

        # If was encrypted, decrypt
        # TODO: Separate transport and dchan encryption types
        if (self.config["crypto"] == "s"):
            #from aes.aes import aes
            from Crypto.Cipher import AES

            self.callback(nick, "dec", "AES")
            #aes_crypto = aes()
            #aes_crypto.setKey(self.config["passwd"])
            # Use CFB mode because it doesn't require padding
            aes_crypto = AES.new(self.config["passwd"], AES.MODE_CFB)

            # Error: I/O operation on closed file?
            self.senders[nick]["fh"].flush()
            self.senders[nick]["fh"].seek(0)
            data = self.senders[nick]["fh"].read()
 
            print "About to decrypt"+str(len(data))+"bytes" 
            data = aes_crypto.decrypt(data)

            out = open(self.config["dl_dir"] + self.senders[nick]["fn"], "wb")
            out.write(data)
            self.senders[nick]["fh"] = out

        self.sendmsg(nick, "sumi done")

        duration = time.time() - self.senders[nick]["start"]
        self.senders[nick]["fh"].close()
        self.callback(nick, "fin", duration, self.senders[nick]["size"], \
              self.senders[nick]["size"] / duration / 1024, \
              self.senders[nick]["all_lost"])
        
        #print "Transfer complete in %.6f seconds" % (duration)
        #print "All lost packets: ", self.senders[nick]["all_lost"]
        #print str(self.senders[nick]["size"]) + " at " + str(
        #     self.senders[nick]["size"] / duration / 1024) + " KB/s"
        self.senders.pop(nick)    # delete the server key
        sys.exit(0) # here now for one file xfer per program

    def thread_recv_packets(self):
        """Receive anonymous packets."""
        print "THREAD 1 - PACKET RECV"
        if (self.config["data_chan_type"] == "u"):
            self.server_udp()
        elif (self.config["data_chan_type"] == "e"):
            self.server_icmp()
        elif (self.config["data_chan_type"] == "i"):
            self.server_icmp()

    def server_icmp(self):
        """Receive ICMP packets. Requires raw sockets."""
        print "ICMP started."   # At the moment, needs to be ran as root
        #print "UID=", os.getuid()
        sock = socket.socket(socket.AF_INET, socket.SOCK_RAW, \
                             socket.IPPROTO_ICMP)
        # XXX: TODO: Fix ICMP. The server doesn't generate ICMP checksums,
        # so we won't be able to receive here!
        sock.bind((self.localaddr, 0))
        while 1:
            (data, addr) = sock.recvfrom(65535)
            data = data[20 + 8:]     # IPHDRSZ + ICMPHDRSZ, get to payload
            self.handle_packet(data, addr)

    def server_udp(self):
        """Receive UDP packets."""
        print "UDP started."
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        while 1:
            try:
                sock.bind((self.localaddr, self.myport))
            except socket.error:
                failed = 0
                if (sys.exc_info()[1].args[0] == 48):
                    print "Port",self.myport,"in use, trying next"
                    self.myport += 1
                    failed = 1
                if not failed: break
        print "Bound to ", self.myport
 
        while 1:
            # This is interrupted...?
            try:
                (data, addr) = sock.recvfrom(65535)
            except socket.error:
                if sys.exc_info()[1].args[0] != 4: # Interrupted system call
                    raise sys.exc_info()[1]     # not for us to catch 
                # Reinvoke it
                continue 
            self.handle_packet(data, addr)

    def cli_user_input(self):
        """Client user input (not used anymore), belongs in separate program."""
        input_lock.acquire()   # wait for messaging program to be connected
        print "Started user input thread... you may now type"
        while 1:
            line = sys.stdin.readline()
            if line == "":    # EOF, exit
                return 0
            line = line[:-1]  # Strip \n
            if line == "":    # blank line (\n), ignore
                continue
            args = line.split()
            if (args[0] == "/get"):
                try:
                    self.request(args[1], args[2])
                except IndexError:
                    print "Usage: get <server_nick> <file>"
            else:
                self.sendmsg(irc_chan, line)
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
    def secure_chan(self, server_nick):
        """Request a secure channel."""
        if (self.config["crypto"] == "a"):
            # TODO: This key generation should be done elsewhere and better
            from Crypto.PublicKey import RSA
            from Crypto.Util.randpool import RandomPool
            from Crypto.Util import number             
            self.pool = RandomPool(384)
            self.pool.stir()
            # Larger key needed to encrypt larger data, 768 too small
            print "Generating key..."
            import cPickle
            # Only send public key, not private key
            # TODO: this needs to be fixed, its not seamless
            self.config["passwd"] = \
                cPickle.dumps(RSA.generate(1024, self.pool.get_bytes).publickey())

        msg = "sumi sec " + \
            self.config["crypto"] + \
            base64.encodestring(self.config["passwd"]).replace("\n", "")

        # Bootstrap sendmsg 
        #self.sendmsg(server_nick, msg)
        self.senders[server_nick]["sendmsg"](server_nick, msg)
        # Now, communicate in crypto with server_nick
        self.senders[server_nick]["crypto"] = self.config["crypto"]
        self.senders[server_nick]["passwd"] = self.config["passwd"]

#    def sendmsg(self, nick, msg):
#        """Send a message over the covert channel using loaded module."""
#        #print "SENDING |%s| to |%s|" % (msg, nick)
#        #return sendmsg(nick, msg)
#        return self.senders[nick]["sendmsg"](nick, msg)

    # Send a secure message to server_nick
    def sendmsg(self, server_nick, msg):
        is_enc = 0
        #print "===", self.senders[server_nick]
        print ">>%s>%s" % (server_nick, msg)
        if (self.senders.has_key(server_nick) and \
            self.senders[server_nick]["crypto"] == "s"):
            from Crypto.Cipher import AES
            #from aes.aes import aes
            #aes_crypto = aes()
            #aes_crypto.setKey(self.senders[server_nick]["passwd"])
            #msg = aes_crypto.encrypt(msg)
            aes_crypto = AES.new(self.senders[server_nick]["passwd"], AES.MODE_CFB)
            msg = aes_crypto.encrypt(msg)
            is_enc = 1
        elif (self.senders.has_key(server_nick) and \
              self.senders[server_nick]["crypto"] == "a"):
            import cPickle
            from Crypto.PublicKey import RSA
            key = cPickle.loads(self.config["passwd"])
            msg = key.encrypt(msg, number.getPrime(10, self.pool.get_bytes))[0]
            is_enc = 1

        # If encrypted, base64 it for transport
        if is_enc:
            msg = base64.encodestring(msg)
            # base64 likes to split the lines, remove newlines we'll do it
            # ourself thanks
            msg = msg.replace("\n", "")

        #print "<<<<<<%s>>>>>>" % msg
        # Note, this message will usually be long; the transport takes
        # care of splitting it up for us if necessary
        ##self.sendmsg(server_nick, msg)
        return self.senders[server_nick]["sendmsg"](server_nick, msg)
       
    def request(self, transport, server_nick, file):
        """Request a file from a server."""
        global transports

        # command line args are now the sole form of user input;
        self.callback(server_nick, "t_wait")   # transport waiting, see below

        # Input lock is mostly obsolete -- it is supposed to wait for
        # transport_init() to return, but we already wait for it 
        #input_lock.acquire()   # wait for transport connection

        if (self.senders.has_key(server_nick)):
            print "Already have a transfer from ",server_nick
            return

        self.senders[server_nick] = {} 

        # Setup transport system
        self.senders[server_nick]["transport"] = transport
        self.load_transport(transport, server_nick)
        if (transports.has_key(transport) and transports[transport]):
            pass    # already initialized
            print "Not initing ",transport
        else:
            self.senders[server_nick]["transport_init"]()
            transports[transport] = 1   # Initialize only once
            print "Just inited",transport

        # Setup cryptology
        self.secure_chan(server_nick)

        print "You want %s from %s" % (server_nick, file)

        offset = 0
        prefix = string.join(map(chr, (random.randint(0, 255),
                                       random.randint(0, 255),
                                       random.randint(0, 255))), "")
 
        self.senders[server_nick]["prefix"] = prefix

        msg = "sumi send " + pack_args({"f":file,
            "o":offset, "i":self.myip, "n":self.myport, "m":self.mss,
            "p":base64.encodestring(prefix)[:-1], "b":self.bandwidth,
            "w":self.rwinsz, "d":self.config["data_chan_type"],
            "x":self.config["crypto"]})
            # XYZ: In sumi sec
            #"K":base64.encodestring(self.config["passwd"])[:-1]})


        self.sendmsg(server_nick, msg) 
        #self.sendmsg(server_nick, msg)
        print "Sent"
        self.callback(server_nick, "req_sent") # request sent (handshaking)

        # Countdown. This provides a timeout for handshaking with nonexistant
        # senders, so the user isn't left hanging.
        maxwait = self.config["maxwait"]

        for x in range(maxwait, 0, -1):
            # If received fn in this time, then exists, so stop countdown
            if (self.senders[server_nick].has_key("fn")):
                return      # don't break - otherwise will timeout
            self.callback(server_nick, "req_count", x)
            time.sleep(1)

        self.callback(server_nick, "timeout")

    # The sole request. In a separate thread so it can wait for IRC.
    # NOTE, sumigetw doesn't use this, it makes its own thread & calls request
    def thread_request(self, nick, file):
         self.request(nick, file)

    def set_callback(self, f):
        """Set callback to be used for handling notifications."""
        self.callback = f

    def default_cb(self, cmd, *args):
        print "(CB)" + cmd + ":" + ",".join(list(map(str, args)))

    def load_transport(self, transport, nick):
        global input_lock, sendmsg, transport_init
        # Import the transport. This may fail, if, for example, there is
        # no such transport module.
        print sys.path
        try:
            t = __import__("transport.mod" + transport, None, None,
                           ["transport_init", "sendmsg"])
        except ImportError:
            self.callback(nick, "t_fail", sys.exc_info())
            return

        t.segment = segment

        self.senders[nick]["sendmsg"] = t.sendmsg
        #sendmsg = t.sendmsg
        #transport_init = t.transport_init

        #self.transport_init = transport_init

        self.senders[nick]["transport_init"] = t.transport_init

        # Wait for transport
        #input_lock.acquire()
   # moved to caller

    def main(self, transport, nick, file):

        #load_transport(transport)

        thread.start_new_thread(self.thread_timer, ())
        senders[nick]["transport_init"] = t.transport_init
        thread.start_new_thread(self.thread_request, (nick, file))

        # This thread will release() input_lock, letting thread_request to go
        #transport_init()

        print "RELEASED"
        input_lock.release()

        # Main thread is UDP server. There is no transport thread, its sendmsg
        self.thread_recv_packets()
   # start waiting before requesting

    def on_exit(self):    # GUI uses this on_exit
        print "Cleaning up..."
        import pprint

        savefile = open(config_file, "w")
        savefile.write("# Client configuration file\n")
        savefile.write("# Please note - ALL COMMENTS IN THIS FILE WILL BE DESTROYED\n")
        # ^ I place all comments in config.py.default instead, or the docs
        pprint.pprint(self.config, savefile)
        savefile.close()

        sys.exit()

def on_sigusr1(signo, intsf):
    global base_path
    print "Got SIGUSR1, calling"
    (transport, nick, file) = open(base_path + "run", 
        "rb").readline().split("\t")
    print "-> ",transport,nick,file
    # TODO: this needs to be more modualized; sendmsg into a server[]
    # and the servers should only be ran once!
    #Client().main(transport, nick, file)
    # TODO: it needs to be possible to run multiple xfers per program, fix it
    #client.main(transport, nick, file)   # Runs servers twice (init_t + udp)
    #client.request(nick, file)    # Interrupted system call recvfrom(65535)
    #sys.exit(0)

# CLI uses this on-exit
def on_exit(signo=0, intsf=0):
    print "Cleaning up..."
    #os.unlink(base_path + "sumiget.pid") 

def pre_main(invoke_req_handler):
    """Before creating the client, this function handles multiple instances."""
    global client

    import signal
    import sys
    # Multi-client support
    if sys.platform == 'win32':
        # win32gui.PumpWaitingMessages()?   # will be handled by wxWindows
        pass 
    else:
        signal.signal(signal.SIGUSR1, invoke_req_handler)    # set handler
    signal.signal(signal.SIGINT, on_exit)

    if (len(sys.argv) >= 3):
        transport = sys.argv[1] 
        nick, file = sys.argv[2], sys.argv[3]
    else:
        print "Usage: sumiget <transport> <nick> <file>"
        sys.exit(-1)

    # 0=seperate program per transfer, 1=all in one (currently not working)
   # =1 works in Unix using signals, but have to resize the frame.
   # TODO: XXX: Get all-in-one working on Win32 with receive/send ing messages
    multiple_instances = 1

    if (multiple_instances and os.access(base_path + "sumiget.pid", os.F_OK)):
        # TODO: file locking so will be unlocked if crashes
        # PID file exists, program (should) be running
        # So signal it, pass control onto - it does work, not us
        master = open(base_path+"sumiget.pid","rb").read()
        if (len(master) != 0):   # If empty, be master
            open(base_path + "run", "wb").write("%s\t%s\t%s" % \
                (transport, nick, file))
            my_master = int(master)
            print "Passing to: ", my_master
            failed = 0 
            try:
                if (sys.platform == 'win32'):
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
            print "Failed to pass to ", my_master
            #sys.exit(-1)
        # File locking would be good; if locked then write cmdline, os.kill other
    # Moved to GUI
    #pidf = open(base_path + "sumiget.pid", "wb")
    #pidf.write(str(os.getpid()))
    #pidf.close() 

# Save pid. This was used for local IPC, but now sockets are used
#def save_pid(pid):
#    pidf = open(base_path + "sumiget.pid", "wb")
#    pidf.write(str(pid))
#    pidf.close()

if __name__ == "__main__":
    pre_main(on_sigusr1)

    #save_pid(os.getpid())

    transport, nick, file = sys.argv[1], sys.argv[2], sys.argv[3]

    print "Getting <%s> from <%s> using <%s>..." % (file, nick, transport)

    try:
        client = Client()
        client.main(transport, nick, file)
    except KeyboardInterrupt, SystemExit:
        on_exit(None, None)
