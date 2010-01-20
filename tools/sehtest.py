#!~/.wine/drive_c/Python25/python.exe

# Acknowledgements:
#  Nicolas Economou, for his ptool suite on which this tool is inspired.
#  http://tinyurl.com/nicolaseconomou

# Bruteforce valid addresses for an SEH overwrite buffer overflow
# Copyright (c) 2009, Mario Vilas
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
#     * Redistributions of source code must retain the above copyright notice,
#       this list of conditions and the following disclaimer.
#     * Redistributions in binary form must reproduce the above copyright
#       notice,this list of conditions and the following disclaimer in the
#       documentation and/or other materials provided with the distribution.
#     * Neither the name of the copyright holder nor the names of its
#       contributors may be used to endorse or promote products derived from
#       this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT OWNER OR CONTRIBUTORS BE
# LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
# CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
# SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
# INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
# CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
# ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.

__revision__ = "$Id$"

import os
import sys
import optparse

import winappdbg
from winappdbg import win32
from winappdbg import Debug, EventHandler, System, Process, MemoryAddresses
from winappdbg import DataAddressIterator, ExecutableAddressIterator
from winappdbg import HexInput, HexDump, CrashDump, Logger

debug_prints = False

#------------------------------------------------------------------------------

class Bruteforcer(EventHandler):

    protect_conversions = {
        win32.PAGE_EXECUTE_READWRITE:   win32.PAGE_EXECUTE_READ,
        win32.PAGE_EXECUTE_WRITECOPY:   win32.PAGE_EXECUTE_READ,
        win32.PAGE_READWRITE:           win32.PAGE_READONLY,
        win32.PAGE_WRITECOPY:           win32.PAGE_READONLY,
    }

    def __init__(self, options):
        super(Bruteforcer, self).__init__()
        self.options = options
        self.testing = False

        if options.output:
            self.output_file = open(options.output, 'a+t')

    def create_process(self, event):
        self.debug   = event.debug
        self.pid     = event.get_pid()
        self.process = event.get_process()
        self.create_thread(event)

    def create_thread(self, event):
        tid = event.get_tid()

    def exception(self, event):
        if event.is_first_chance():
            event.continueStatus = win32.DBG_EXCEPTION_NOT_HANDLED
            if self.testing:
                if self.checkSnapshotPage(event):
                    if debug_prints:
                        print "updated snapshot"
                    event.continueStatus = win32.DBG_CONTINUE
                elif hasattr(event, 'get_fault_type') and event.get_fault_type() == win32.EXCEPTION_EXECUTE_FAULT:
                    # XXX FIXME
                    # if the crash is a DEP fault I get stuck here
                    # forever and no SEH is used at all!
                    # more research is needed to see if it's indeed a
                    # protection of the OS or a bug in my debugger :/
                    if debug_prints:
                        print "dep fault, aborting"
                    self.process.kill()
##                elif event.get_exception_address() == self.triggered_pc:
##                    if debug_prints:
##                        print "ignored exception"
                    # XXX FIXME
                    # I don't know why I get these extra exceptions :(
                else:
                    self.nextAddress()
            elif self.findAttackerExceptionHandler(event):
                if debug_prints:
                    print "found attacker seh"
                self.testing = True
                self.beginTesting(event)
                self.nextAddress()
        else:
            event.continueStatus = win32.DBG_EXCEPTION_HANDLED
            if not self.testing:
##                if debug_prints:
##                    print "jumped to attacker seh"
##                self.testing = True
##                self.beginTesting(event)
##                self.nextAddress()
                if debug_prints:
                    print "got a crash but seh is intact, aborting"
                self.process.kill()
            else:
                if self.checkSnapshotPage(event):
                    if debug_prints:
                        print "updated snapshot"
                else:
                    if debug_prints:
                        print "ignored second chance"

    def beginTesting(self, event):
        if debug_prints:
            print "begin testing"

        self.tid    = event.get_tid()
        self.thread = event.get_thread()

        self.iter = ExecutableAddressIterator(self.process.get_memory_map())
        self.current_target = None

        try:
            self.triggered_pc = event.get_exception_address()
        except AttributeError:
            self.triggered_pc = self.thread.get_pc()

        self.suspendOtherThreads()
        self.rememberExceptionHandler()
        self.takeSnapshot()

    def stopTesting(self):
        if debug_prints:
            print "stop testing"
##        self.restoreSnapshot()
        self.cleanupSnapshot()
        self.restoreExceptionHandler()
        self.resumeOtherThreads()
        self.process.kill()

    def nextAddress(self):
        if debug_prints:
            print "continue testing"
        self.removeBreakpoint()
        try:
            self.current_target = self.iter.next()
            if debug_prints:
                print "next target is %s" % HexDump.address(self.current_target)
            if self.options.output:
                print "Trying: %s" % HexDump.address(self.current_target)
        except StopIteration:
            self.stopTesting()
            return
        self.restoreSnapshot()
        self.changeExceptionHandler()
        self.setBreakpoint()

    def foundValidTarget(self, event):
        if debug_prints:
            print "found valid target"
        printable_address = HexDump.address(self.current_target)
        if self.options.output:
            print "FOUND: %s" % printable_address
            print >> self.output_file, printable_address
            self.output_file.flush()
        else:
            print printable_address
        self.nextAddress()

    def findAttackerExceptionHandler(self, event):
        if debug_prints:
            print "looking for attacker seh"
        attacker_seh = self.process.resolve_label(self.options.seh)
        if debug_prints:
            print "attacker seh would be %s (%s)" % (self.options.seh, HexDump.address(attacker_seh))
        sizeof_pvoid = win32.sizeof(win32.PVOID)
        pfirst   = event.get_thread().get_seh_chain_pointer()
        pcurrent = pfirst
        while pcurrent != 0xFFFFFFFF:
            try:
                pnext = self.process.read_pointer(pcurrent)
                pseh  = self.process.read_pointer(pcurrent + sizeof_pvoid)
            except WindowsError:
                break
            if debug_prints:
                print "looking at seh %s" % HexDump.address(pseh)
            if pseh == attacker_seh:
                return True
            if debug_prints:
                print "current (%s) -> next (%s)" % (HexDump.address(pcurrent), HexDump.address(pnext))
            pcurrent = pnext
        return False

    def setBreakpoint(self):
        if debug_prints:
            print "set breakpoint"
        self.debug.stalk_at(self.pid, self.current_target, self.foundValidTarget)

    def removeBreakpoint(self):
        if debug_prints:
            print "remove breakpoint"
        if self.current_target is not None:
            self.debug.dont_stalk_at(self.pid, self.current_target)

    def rememberExceptionHandler(self):
        if debug_prints:
            print "remember exception handler"
        self.first_seh        = self.thread.get_seh_chain_pointer()
        self.next_seh         = self.process.read_pointer(self.first_seh)
        self.ptr_function_seh = self.first_seh + win32.sizeof(win32.LPVOID)
        self.function_seh     = self.process.read_pointer(self.ptr_function_seh)

    def changeExceptionHandler(self):
        if debug_prints:
            print "change exception handler"
        self.process.write_pointer(self.first_seh, win32.LPVOID(-1).value)
        self.process.write_pointer(self.ptr_function_seh, self.current_target)

    def restoreExceptionHandler(self):
        if debug_prints:
            print "restore exception handler"
        self.process.write_pointer(self.first_seh, self.next_seh)
        self.process.write_pointer(self.ptr_function_seh, self.function_seh)

    def suspendOtherThreads(self):
        if debug_prints:
            print "suspend other threads"
        for thread in self.process.iter_threads():
            if thread.get_tid() != self.tid:
                thread.suspend()

    def resumeOtherThreads(self):
        if debug_prints:
            print "resume other threads"
        for thread in self.process.iter_threads():
            if thread.get_tid() != self.tid:
                thread.resume()

    def takeSnapshot(self):
        if debug_prints:
            print "take snapshot"
        self.context = self.thread.get_context()

        pageSize = System.pageSize

        self.special_pages = dict()
        page = MemoryAddresses.align_address_to_page_start( self.process.get_peb_address() )
        self.special_pages[page] = self.process.read(page, pageSize)
        for thread in self.process.iter_threads():
            page = MemoryAddresses.align_address_to_page_start( thread.get_teb_address() )
            self.special_pages[page] = self.process.read(page, pageSize)

        self.memory = dict()
        self.tainted = set()
        for mbi in self.process.get_memory_map():
            if mbi.is_writeable():
                for page in xrange(mbi.BaseAddress, mbi.BaseAddress + mbi.RegionSize, pageSize):
                    if not self.special_pages.has_key(page):
                        protect = mbi.Protect
                        new_protect = self.protect_conversions[protect]
                        try:
                            self.process.mprotect(page, pageSize, new_protect)
                            self.memory[page] = (None, protect, new_protect)
                        except WindowsError:
                            self.special_pages[page] = self.process.read(page, pageSize)
                            if debug_prints:
                                print "unexpected special page %s" % HexDump.address(page)

    def restoreSnapshot(self):
        if debug_prints:
            print "restore snapshot"
        self.thread.set_context(self.context)
        pageSize = System.pageSize
        process = self.process
        tainted = self.tainted
        for page, content in self.special_pages.iteritems():
            process.write(page, content)
        for page, (content, protect, new_protect) in self.memory.iteritems():
            if page in tainted:
                process.write(page, content)
                process.mprotect(page, pageSize, new_protect)
                tainted.remove(page)

    def checkSnapshotPage(self, event):
        if event.get_tid() == self.tid:
            try:
                fault_type = event.get_fault_type()
            except AttributeError:
                fault_type = None
            if fault_type == win32.EXCEPTION_WRITE_FAULT:
                address = event.get_fault_address()
                page = MemoryAddresses.align_address_to_page_start(address)
                if self.memory.has_key(page):
                    (content, protect, new_protect) = self.memory[page]
                    content = self.process.read(page, System.pageSize)
                    self.memory[page] = (content, protect, new_protect)
                    self.tainted.add(page)
                    self.process.mprotect(page, System.pageSize, protect)
                    return True
        return False

    def cleanupSnapshot(self):
        self.restoreSnapshot()
        pageSize = System.pageSize
        for page, (content, protect, new_protect) in self.memory.iteritems():
            self.process.mprotect(page, pageSize, protect)

#------------------------------------------------------------------------------

class EventForwarder(EventHandler):
    def __init__(self, cls, options):
        self.cls     = cls
        self.options = options
        self.forward = dict()
        super(EventForwarder, self).__init__()

    def log_event(self, event):
        if debug_prints:
            try:
                print HexDump.address(event.get_exception_address()), event.get_exception_description(), event.is_first_chance()
            except AttributeError:
                print HexDump.address(event.get_thread().get_pc()), event.get_event_name()

    def event(self, event):
        self.log_event(event)
        pid = event.get_pid()
        if self.forward.has_key(pid):
            return self.forward[pid](event)

    def create_process(self, event):
        self.log_event(event)
        handler = self.cls(self.options)
        self.forward[event.get_pid()] = handler
        return handler(event)

    def exit_process(self, event):
        self.log_event(event)
        pid = event.get_pid()
        if self.forward.has_key(pid):
            retval = self.forward[pid](event)
            del self.forward[pid]
            return retval

    def breakpoint(self, event):
        event.continueStatus = win32.DBG_EXCEPTION_HANDLED
        self.log_event(event)

    def wow64_breakpoint(self, event):
        event.continueStatus = win32.DBG_EXCEPTION_HANDLED
        self.log_event(event)

    def debug_control_c(self, event):
        event.continueStatus = win32.DBG_EXCEPTION_HANDLED
        self.log_event(event)

    def invalid_handle(self, event):
        event.continueStatus = win32.DBG_EXCEPTION_HANDLED
        self.log_event(event)

    def possible_deadlock(self, event):
        event.continueStatus = win32.DBG_EXCEPTION_HANDLED
        self.log_event(event)

#------------------------------------------------------------------------------

def main( argv ):

    # Parse the command line arguments
    options = parse_cmdline(argv)

    # Create the event handler object
    eventHandler = EventForwarder(Bruteforcer, options)

    # Create the debug object
    debug = Debug(eventHandler, bKillOnExit = True)
    try:

        # Attach to the targets
        for dwProcessId in options.attach:
            debug.attach(dwProcessId)
        for lpCmdLine in options.console:
            debug.execl(lpCmdLine, bConsole = True,  bFollow = options.follow)
        for lpCmdLine in options.windowed:
            debug.execl(lpCmdLine, bConsole = False, bFollow = options.follow)

        # Run the debug loop
        debug.loop()

    # Stop the debugger
    finally:
        debug.stop()

#------------------------------------------------------------------------------

def parse_cmdline( argv ):

    # Help message and version string
    version = (
              "Bruteforce valid addresses for an SEH overwrite buffer overflow\n"
              "by Mario Vilas (mvilas at gmail.com)\n"
              "%s\n"
              ) % winappdbg.version
    usage = (
            "\n"
            "\n"
            "  Create a new process (parameters for the target must be escaped):\n"
            "    %prog [options] -c <executable> [parameters for the target]\n"
            "    %prog [options] -w <executable> [parameters for the target]\n"
            "\n"
            "  Attach to a running process (by filename):\n"
            "    %prog [options] -a <executable>\n"
            "\n"
            "  Attach to a running process (by ID):\n"
            "    %prog [options] -a <process id>"
            )
    parser = optparse.OptionParser(
                                    usage=usage,
                                    version=version,
                                  )

    # Commands
    commands = optparse.OptionGroup(parser, "Commands")
    commands.add_option("-a", "--attach", action="append",
                        help="Attach to a running process")
    commands.add_option("-w", "--windowed", action="append",
                        help="Create a new windowed process")
    commands.add_option("-c", "--console", action="append",
                        help="Create a new console process [default]")
    parser.add_option_group(commands)

    # SEH test options
    sehtest = optparse.OptionGroup(parser, "SEH Test options")
    sehtest.add_option("--seh", metavar="ADDRESS",
                       help="address of SEH handler function to hijack [default: 0x41414141]")
    sehtest.add_option("-o", "--output", metavar="FILE",
                       help="write the output to FILE")
    parser.add_option_group(sehtest)

    # Debugging options
    debugging = optparse.OptionGroup(parser, "Debugging options")
    debugging.add_option("--follow", action="store_true",
                  help="automatically attach to child processes [default]")
    debugging.add_option("--dont-follow", action="store_false",
                                                             dest="follow",
                  help="don't automatically attach to child processes")
    parser.add_option_group(debugging)

    # Defaults
    parser.set_defaults(
        follow      = True,
        windowed    = list(),
        console     = list(),
        attach      = list(),
        seh         = '0x41414141',
    )

    # Parse and validate the command line options
    if len(argv) == 1:
        argv = argv + [ '--help' ]
    (options, args) = parser.parse_args(argv)
    args = args[1:]
    if not options.windowed and not options.console and not options.attach:
        options.console = [ System.argv_to_cmdline(args) ]
    else:
        if args:
            parser.error("don't know what to do with extra parameters: %s" % args)

    # Get the list of attach targets
    system = System()
    system.request_debug_privileges()
    system.scan_processes()
    attach_targets = list()
    for token in options.attach:
        try:
            dwProcessId = HexInput.integer(token)
        except ValueError:
            dwProcessId = None
        if dwProcessId is not None:
            if not system.has_process(dwProcessId):
                parser.error("can't find process %d" % dwProcessId)
            try:
                process = Process(dwProcessId)
                process.open_handle()
                process.close_handle()
            except WindowsError, e:
                parser.error("can't open process %d: %s" % (dwProcessId, e))
            attach_targets.append(dwProcessId)
        else:
            matched = system.find_processes_by_filename(token)
            if not matched:
                parser.error("can't find process %s" % token)
            for process, name in matched:
                dwProcessId = process.get_pid()
                try:
                    process = Process(dwProcessId)
                    process.open_handle()
                    process.close_handle()
                except WindowsError, e:
                    parser.error("can't open process %d: %s" % (dwProcessId, e))
                attach_targets.append( process.get_pid() )
    options.attach = attach_targets

    # Get the list of console programs to execute
    console_targets = list()
    for token in options.console:
        vector = system.cmdline_to_argv(token)
        if not vector:
            parser.error("bad use of --console")
        filename = vector[0]
        if not os.path.exists(filename):
            try:
                filename = win32.SearchPath(None, filename, '.exe')[0]
            except WindowsError, e:
                parser.error("error searching for %s: %s" % (filename, str(e)))
            vector[0] = filename
            token     = system.argv_to_cmdline(vector)
        console_targets.append(token)
    options.console = console_targets

    # Get the list of windowed programs to execute
    windowed_targets = list()
    for token in options.windowed:
        vector = system.cmdline_to_argv(token)
        if not vector:
            parser.error("bad use of --windowed")
        filename = vector[0]
        if not os.path.exists(filename):
            try:
                filename = win32.SearchPath(None, filename, '.exe')[0]
            except WindowsError, e:
                parser.error("error searching for %s: %s" % (filename, str(e)))
            vector[0] = filename
            token     = system.argv_to_cmdline(vector)
        windowed_targets.append(token)
    options.windowed = windowed_targets

    # If no targets were set at all, show an error message
    if not options.attach and not options.console and not options.windowed:
        parser.error("no targets found!")

    return options

#------------------------------------------------------------------------------

if __name__ == "__main__":
    try:
        import psyco
        psyco.bind(main)
    except ImportError:
        pass
    main(sys.argv)
