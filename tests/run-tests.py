#! /usr/bin/env python3

import os
import subprocess
import sys
import sysconfig
import platform
import argparse
import inspect
import json
import re
from glob import glob
import multiprocessing
from multiprocessing.pool import ThreadPool
import threading
import tempfile

# Maximum time to run a PC-based test, in seconds.
TEST_TIMEOUT = 30

# See stackoverflow.com/questions/2632199: __file__ nor sys.argv[0]
# are guaranteed to always work, this one should though.
BASEPATH = os.path.dirname(os.path.abspath(inspect.getsourcefile(lambda: None)))


def base_path(*p):
    return os.path.abspath(os.path.join(BASEPATH, *p)).replace("\\", "/")


# Tests require at least CPython 3.3. If your default python3 executable
# is of lower version, you can point MICROPY_CPYTHON3 environment var
# to the correct executable.
if os.name == "nt":
    CPYTHON3 = os.getenv("MICROPY_CPYTHON3", "python")
    MICROPYTHON = os.getenv(
        "MICROPY_MICROPYTHON", base_path("../ports/windows/build-standard/micropython.exe")
    )
    # mpy-cross is only needed if --via-mpy command-line arg is passed
    MPYCROSS = os.getenv("MICROPY_MPYCROSS", base_path("../mpy-cross/build/mpy-cross.exe"))
else:
    CPYTHON3 = os.getenv("MICROPY_CPYTHON3", "python3")
    MICROPYTHON = os.getenv(
        "MICROPY_MICROPYTHON", base_path("../ports/unix/build-standard/micropython")
    )
    # mpy-cross is only needed if --via-mpy command-line arg is passed
    MPYCROSS = os.getenv("MICROPY_MPYCROSS", base_path("../mpy-cross/build/mpy-cross"))

# Use CPython options to not save .pyc files, to only access the core standard library
# (not site packages which may clash with u-module names), and improve start up time.
CPYTHON3_CMD = [CPYTHON3, "-BS"]

# File with the test results.
RESULTS_FILE = "_results.json"

# For diff'ing test output
DIFF = os.getenv("MICROPY_DIFF", "diff -u")

# Set PYTHONIOENCODING so that CPython will use utf-8 on systems which set another encoding in the locale
os.environ["PYTHONIOENCODING"] = "utf-8"

# Code to allow a target MicroPython to import an .mpy from RAM
injected_import_hook_code = """\
import sys, os, io, vfs
class __File(io.IOBase):
  def __init__(self):
    self.off = 0
  def ioctl(self, request, arg):
    return 0
  def readinto(self, buf):
    buf[:] = memoryview(__buf)[self.off:self.off + len(buf)]
    self.off += len(buf)
    return len(buf)
class __FS:
  def mount(self, readonly, mkfs):
    pass
  def umount(self):
    pass
  def chdir(self, path):
    pass
  def stat(self, path):
    if path == '__injected_test.mpy':
      return tuple(0 for _ in range(10))
    else:
      raise OSError(-2) # ENOENT
  def open(self, path, mode):
    return __File()
vfs.mount(__FS(), '/__vfstest')
os.chdir('/__vfstest')
__import__('__injected_test')
"""


def rm_f(fname):
    if os.path.exists(fname):
        os.remove(fname)


# unescape wanted regex chars and escape unwanted ones
def convert_regex_escapes(line):
    cs = []
    escape = False
    for c in str(line, "utf8"):
        if escape:
            escape = False
            cs.append(c)
        elif c == "\\":
            escape = True
        elif c in ("(", ")", "[", "]", "{", "}", ".", "*", "+", "^", "$"):
            cs.append("\\" + c)
        else:
            cs.append(c)
    # accept carriage-return(s) before final newline
    if cs[-1] == "\n":
        cs[-1] = "\r*\n"
    return bytes("".join(cs), "utf8")


def prepare_script_for_target(args, *, script_filename=None, script_text=None, force_plain=False):
    if force_plain or (not args.via_mpy and args.emit == "bytecode"):
        if script_filename is not None:
            with open(script_filename, "rb") as f:
                script_text = f.read()
    elif args.via_mpy:
        tempname = tempfile.mktemp(dir="")
        mpy_filename = tempname + ".mpy"

        if script_filename is None:
            script_filename = tempname + ".py"
            cleanup_script_filename = True
            with open(script_filename, "wb") as f:
                f.write(script_text)
        else:
            cleanup_script_filename = False

        try:
            subprocess.check_output(
                [MPYCROSS]
                + args.mpy_cross_flags.split()
                + ["-o", mpy_filename, "-X", "emit=" + args.emit, script_filename],
                stderr=subprocess.STDOUT,
            )
        except subprocess.CalledProcessError as er:
            return True, b"mpy-cross crash\n" + er.output

        with open(mpy_filename, "rb") as f:
            script_text = b"__buf=" + bytes(repr(f.read()), "ascii") + b"\n"

        rm_f(mpy_filename)
        if cleanup_script_filename:
            rm_f(script_filename)

        script_text += bytes(injected_import_hook_code, "ascii")
    else:
        print("error: using emit={} must go via .mpy".format(args.emit))
        sys.exit(1)

    return False, script_text


def run_script_on_remote_target(pyb, args, test_file, is_special):
    had_crash, script = prepare_script_for_target(
        args, script_filename=test_file, force_plain=is_special
    )
    if had_crash:
        return True, script

    try:
        had_crash = False
        pyb.enter_raw_repl()
        output_mupy = pyb.exec_(script)
    except pyboard.PyboardError as e:
        had_crash = True
        if not is_special and e.args[0] == "exception":
            output_mupy = e.args[1] + e.args[2] + b"CRASH"
        else:
            output_mupy = bytes(e.args[0], "ascii") + b"\nCRASH"
    return had_crash, output_mupy


special_tests = [
    base_path(file)
    for file in (
        "micropython/meminfo.py",
        "basics/bytes_compare3.py",
        "basics/builtin_help.py",
        "thread/thread_exc2.py",
        "esp32/partition_ota.py",
        "circuitpython/traceback_test.py",  # CIRCUITPY-CHANGE
        "circuitpython/traceback_test_chained.py",  # CIRCUITPY-CHANGE
    )
]


def run_micropython(pyb, args, test_file, test_file_abspath, is_special=False):
    had_crash = False
    if pyb is None:
        # run on PC
        if (
            test_file_abspath.startswith((base_path("cmdline/"), base_path("feature_check/")))
            or test_file_abspath in special_tests
        ):
            # special handling for tests of the unix cmdline program
            is_special = True

        if is_special:
            # check for any cmdline options needed for this test
            args = [MICROPYTHON]
            with open(test_file, "rb") as f:
                line = f.readline()
                if line.startswith(b"# cmdline:"):
                    # subprocess.check_output on Windows only accepts strings, not bytes
                    args += [str(c, "utf-8") for c in line[10:].strip().split()]

            # run the test, possibly with redirected input
            try:
                if os.path.basename(test_file).startswith("repl_"):
                    # Need to use a PTY to test command line editing
                    try:
                        import pty
                    except ImportError:
                        # in case pty module is not available, like on Windows
                        return b"SKIP\n"
                    import select

                    # Even though these might have the pty module, it's unlikely to function.
                    if sys.platform in ["win32", "msys", "cygwin"]:
                        return b"SKIP\n"

                    def get(required=False):
                        rv = b""
                        while True:
                            ready = select.select([master], [], [], 0.02)
                            if ready[0] == [master]:
                                rv += os.read(master, 1024)
                            else:
                                if not required or rv:
                                    return rv

                    def send_get(what):
                        os.write(master, what)
                        return get()

                    with open(test_file, "rb") as f:
                        # instead of: output_mupy = subprocess.check_output(args, stdin=f)
                        master, slave = pty.openpty()
                        p = subprocess.Popen(
                            args, stdin=slave, stdout=slave, stderr=subprocess.STDOUT, bufsize=0
                        )
                        banner = get(True)
                        output_mupy = banner + b"".join(send_get(line) for line in f)
                        send_get(b"\x04")  # exit the REPL, so coverage info is saved
                        # At this point the process might have exited already, but trying to
                        # kill it 'again' normally doesn't result in exceptions as Python and/or
                        # the OS seem to try to handle this nicely. When running Linux on WSL
                        # though, the situation differs and calling Popen.kill after the process
                        # terminated results in a ProcessLookupError. Just catch that one here
                        # since we just want the process to be gone and that's the case.
                        try:
                            p.kill()
                        except ProcessLookupError:
                            pass
                        os.close(master)
                        os.close(slave)
                else:
                    output_mupy = subprocess.check_output(
                        args + [test_file], stderr=subprocess.STDOUT
                    )
            except subprocess.CalledProcessError:
                return b"CRASH"

        else:
            # a standard test run on PC

            # create system command
            cmdlist = [os.path.abspath(MICROPYTHON), "-X", "emit=" + args.emit]
            if args.heapsize is not None:
                cmdlist.extend(["-X", "heapsize=" + args.heapsize])
            if sys.platform == "darwin":
                cmdlist.extend(["-X", "realtime"])

            cwd = os.path.dirname(test_file)

            # if running via .mpy, first compile the .py file
            if args.via_mpy:
                mpy_filename = tempfile.mktemp(dir=cwd, suffix=".mpy")
                subprocess.check_output(
                    [MPYCROSS]
                    + args.mpy_cross_flags.split()
                    + ["-o", mpy_filename, "-X", "emit=" + args.emit, test_file]
                )
                mpy_modname = os.path.splitext(os.path.basename(mpy_filename))[0]
                cmdlist.extend(["-m", mpy_modname])
            else:
                cmdlist.append(test_file_abspath)

            # run the actual test
            try:
                output_mupy = subprocess.check_output(
                    cmdlist, stderr=subprocess.STDOUT, timeout=TEST_TIMEOUT, cwd=cwd
                )
            except subprocess.CalledProcessError as er:
                had_crash = True
                output_mupy = er.output + b"CRASH"
            except subprocess.TimeoutExpired as er:
                had_crash = True
                output_mupy = (er.output or b"") + b"TIMEOUT"

            # clean up if we had an intermediate .mpy file
            if args.via_mpy:
                rm_f(mpy_filename)

    else:
        # run via pyboard interface
        had_crash, output_mupy = pyb.run_script_on_remote_target(
            args, test_file_abspath, is_special
        )

    # canonical form for all ports/platforms is to use \n for end-of-line
    output_mupy = output_mupy.replace(b"\r\n", b"\n")

    # don't try to convert the output if we should skip this test
    if had_crash or output_mupy in (b"SKIP\n", b"CRASH"):
        return output_mupy

    # skipped special tests will output "SKIP" surrounded by other interpreter debug output
    if is_special and not had_crash and b"\nSKIP\n" in output_mupy:
        return b"SKIP\n"

    if is_special or test_file_abspath in special_tests:
        # convert parts of the output that are not stable across runs
        with open(test_file + ".exp", "rb") as f:
            lines_exp = []
            for line in f.readlines():
                if line == b"########\n":
                    line = (line,)
                else:
                    line = (line, re.compile(convert_regex_escapes(line)))
                lines_exp.append(line)
        lines_mupy = [line + b"\n" for line in output_mupy.split(b"\n")]
        if output_mupy.endswith(b"\n"):
            lines_mupy = lines_mupy[:-1]  # remove erroneous last empty line
        i_mupy = 0
        for i in range(len(lines_exp)):
            if lines_exp[i][0] == b"########\n":
                # 8x #'s means match 0 or more whole lines
                line_exp = lines_exp[i + 1]
                skip = 0
                while i_mupy + skip < len(lines_mupy) and not line_exp[1].match(
                    lines_mupy[i_mupy + skip]
                ):
                    skip += 1
                if i_mupy + skip >= len(lines_mupy):
                    lines_mupy[i_mupy] = b"######## FAIL\n"
                    break
                del lines_mupy[i_mupy : i_mupy + skip]
                lines_mupy.insert(i_mupy, b"########\n")
                i_mupy += 1
            else:
                # a regex
                if lines_exp[i][1].match(lines_mupy[i_mupy]):
                    lines_mupy[i_mupy] = lines_exp[i][0]
                else:
                    # print("don't match: %r %s" % (lines_exp[i][1], lines_mupy[i_mupy])) # DEBUG
                    pass
                i_mupy += 1
            if i_mupy >= len(lines_mupy):
                break
        output_mupy = b"".join(lines_mupy)

    return output_mupy


def run_feature_check(pyb, args, test_file):
    if pyb is not None and test_file.startswith("repl_"):
        # REPL feature tests will not run via pyboard because they require prompt interactivity
        return b""
    test_file_path = base_path("feature_check", test_file)
    return run_micropython(pyb, args, test_file_path, test_file_path, is_special=True)


class ThreadSafeCounter:
    def __init__(self, start=0):
        self._value = start
        self._lock = threading.Lock()

    def increment(self):
        self.add(1)

    def add(self, to_add):
        with self._lock:
            self._value += to_add

    def append(self, arg):
        self.add([arg])

    @property
    def value(self):
        return self._value


class PyboardNodeRunner:
    def __init__(self):
        mjs = os.getenv("MICROPY_MICROPYTHON_MJS")
        if mjs is None:
            mjs = base_path("../ports/webassembly/build-standard/micropython.mjs")
        else:
            mjs = os.path.abspath(mjs)
        self.micropython_mjs = mjs

    def close(self):
        pass

    def run_script_on_remote_target(self, args, test_file, is_special):
        cwd = os.path.dirname(test_file)

        # Create system command list.
        cmdlist = ["node"]
        if test_file.endswith(".py"):
            # Run a Python script indirectly via "node micropython.mjs <script.py>".
            cmdlist.append(self.micropython_mjs)
            if args.heapsize is not None:
                cmdlist.extend(["-X", "heapsize=" + args.heapsize])
            cmdlist.append(test_file)
        else:
            # Run a js/mjs script directly with Node, passing in the path to micropython.mjs.
            cmdlist.append(test_file)
            cmdlist.append(self.micropython_mjs)

        # Run the script.
        try:
            had_crash = False
            output_mupy = subprocess.check_output(
                cmdlist, stderr=subprocess.STDOUT, timeout=TEST_TIMEOUT, cwd=cwd
            )
        except subprocess.CalledProcessError as er:
            had_crash = True
            output_mupy = er.output + b"CRASH"
        except subprocess.TimeoutExpired as er:
            had_crash = True
            output_mupy = (er.output or b"") + b"TIMEOUT"

        # Return the results.
        return had_crash, output_mupy


def run_tests(pyb, tests, args, result_dir, num_threads=1):
    test_count = ThreadSafeCounter()
    testcase_count = ThreadSafeCounter()
    passed_count = ThreadSafeCounter()
    failed_tests = ThreadSafeCounter([])
    skipped_tests = ThreadSafeCounter([])

    skip_tests = set()
    skip_native = False
    skip_int_big = False
    skip_bytearray = False
    skip_set_type = False
    skip_slice = False
    skip_async = False
    skip_const = False
    skip_revops = False
    skip_io_module = False
    skip_fstring = False
    skip_endian = False
    has_complex = True
    has_coverage = False

    upy_float_precision = 32

    if True:
        # Even if we run completely different tests in a different directory,
        # we need to access feature_checks from the same directory as the
        # run-tests.py script itself so use base_path.

        # Check if micropython.native is supported, and skip such tests if it's not
        output = run_feature_check(pyb, args, "native_check.py")
        if output != b"native\n":
            skip_native = True

        # Check if arbitrary-precision integers are supported, and skip such tests if it's not
        output = run_feature_check(pyb, args, "int_big.py")
        if output != b"1000000000000000000000000000000000000000000000\n":
            skip_int_big = True

        # Check if bytearray is supported, and skip such tests if it's not
        output = run_feature_check(pyb, args, "bytearray.py")
        if output != b"bytearray\n":
            skip_bytearray = True

        # Check if set type (and set literals) is supported, and skip such tests if it's not
        output = run_feature_check(pyb, args, "set_check.py")
        if output != b"{1}\n":
            skip_set_type = True

        # Check if slice is supported, and skip such tests if it's not
        output = run_feature_check(pyb, args, "slice.py")
        if output != b"slice\n":
            skip_slice = True

        # Check if async/await keywords are supported, and skip such tests if it's not
        output = run_feature_check(pyb, args, "async_check.py")
        if output != b"async\n":
            skip_async = True

        # Check if const keyword (MicroPython extension) is supported, and skip such tests if it's not
        output = run_feature_check(pyb, args, "const.py")
        if output != b"1\n":
            skip_const = True

        # Check if __rOP__ special methods are supported, and skip such tests if it's not
        output = run_feature_check(pyb, args, "reverse_ops.py")
        if output == b"TypeError\n":
            skip_revops = True

        # Check if io module exists, and skip such tests if it doesn't
        output = run_feature_check(pyb, args, "io_module.py")
        if output != b"io\n":
            skip_io_module = True

        # Check if fstring feature is enabled, and skip such tests if it doesn't
        output = run_feature_check(pyb, args, "fstring.py")
        if output != b"a=1\n":
            skip_fstring = True

        # Check if @micropython.asm_thumb supports Thumb2 instructions, and skip such tests if it doesn't
        output = run_feature_check(pyb, args, "inlineasm_thumb2.py")
        if output != b"thumb2\n":
            skip_tests.add("inlineasm/asmbcc.py")
            skip_tests.add("inlineasm/asmbitops.py")
            skip_tests.add("inlineasm/asmconst.py")
            skip_tests.add("inlineasm/asmdiv.py")
            skip_tests.add("inlineasm/asmfpaddsub.py")
            skip_tests.add("inlineasm/asmfpcmp.py")
            skip_tests.add("inlineasm/asmfpldrstr.py")
            skip_tests.add("inlineasm/asmfpmuldiv.py")
            skip_tests.add("inlineasm/asmfpsqrt.py")
            skip_tests.add("inlineasm/asmit.py")
            skip_tests.add("inlineasm/asmspecialregs.py")

        # Check if emacs repl is supported, and skip such tests if it's not
        t = run_feature_check(pyb, args, "repl_emacs_check.py")
        if "True" not in str(t, "ascii"):
            skip_tests.add("cmdline/repl_emacs_keys.py")

        # Check if words movement in repl is supported, and skip such tests if it's not
        t = run_feature_check(pyb, args, "repl_words_move_check.py")
        if "True" not in str(t, "ascii"):
            skip_tests.add("cmdline/repl_words_move.py")

        upy_byteorder = run_feature_check(pyb, args, "byteorder.py")
        upy_float_precision = run_feature_check(pyb, args, "float.py")
        try:
            upy_float_precision = int(upy_float_precision)
        except ValueError:
            upy_float_precision = 0
        has_complex = run_feature_check(pyb, args, "complex.py") == b"complex\n"
        has_coverage = run_feature_check(pyb, args, "coverage.py") == b"coverage\n"
        cpy_byteorder = subprocess.check_output(
            CPYTHON3_CMD + [base_path("feature_check/byteorder.py")]
        )
        skip_endian = upy_byteorder != cpy_byteorder

    # These tests don't test slice explicitly but rather use it to perform the test
    misc_slice_tests = (
        "builtin_range",
        "bytearray1",
        "class_super",
        "containment",
        "errno1",
        "fun_str",
        "generator1",
        "globals_del",
        "memoryview1",
        "memoryview_gc",
        "object1",
        "python34",
        "string_format_modulo",
        "struct_endian",
    )

    # Some tests shouldn't be run on GitHub Actions
    if os.getenv("GITHUB_ACTIONS") == "true":
        skip_tests.add("thread/stress_schedule.py")  # has reliability issues

        if os.getenv("RUNNER_OS") == "Windows" and os.getenv("CI_BUILD_CONFIGURATION") == "Debug":
            # fails with stack overflow on Debug builds
            skip_tests.add("misc/sys_settrace_features.py")

    if upy_float_precision == 0:
        skip_tests.add("extmod/uctypes_le_float.py")
        skip_tests.add("extmod/uctypes_native_float.py")
        skip_tests.add("extmod/uctypes_sizeof_float.py")
        skip_tests.add("extmod/json_dumps_float.py")
        skip_tests.add("extmod/json_loads_float.py")
        skip_tests.add("extmod/random_extra_float.py")
        skip_tests.add("misc/rge_sm.py")
    if upy_float_precision < 32:
        skip_tests.add(
            "float/float2int_intbig.py"
        )  # requires fp32, there's float2int_fp30_intbig.py instead
        skip_tests.add(
            "float/string_format.py"
        )  # requires fp32, there's string_format_fp30.py instead
        skip_tests.add("float/bytes_construct.py")  # requires fp32
        skip_tests.add("float/bytearray_construct.py")  # requires fp32
        skip_tests.add("float/float_format_ints_power10.py")  # requires fp32
    if upy_float_precision < 64:
        skip_tests.add("float/float_divmod.py")  # tested by float/float_divmod_relaxed.py instead
        skip_tests.add("float/float2int_doubleprec_intbig.py")
        skip_tests.add("float/float_format_ints_doubleprec.py")
        skip_tests.add("float/float_parse_doubleprec.py")

    if not has_complex:
        skip_tests.add("float/complex1.py")
        skip_tests.add("float/complex1_intbig.py")
        skip_tests.add("float/complex_reverse_op.py")
        skip_tests.add("float/complex_special_methods.py")
        skip_tests.add("float/int_big_float.py")
        skip_tests.add("float/true_value.py")
        skip_tests.add("float/types.py")
        skip_tests.add("float/complex_dunder.py")

    if not has_coverage:
        skip_tests.add("cmdline/cmd_parsetree.py")
        skip_tests.add("cmdline/repl_sys_ps1_ps2.py")
        skip_tests.add("extmod/ssl_poll.py")

    # Skip thread mutation tests on targets that don't have the GIL.
    if args.target in ("rp2", "unix"):
        for t in tests:
            if t.startswith("thread/mutate_"):
                skip_tests.add(t)

    # Skip thread tests that require many threads on targets that don't support multiple threads.
    if args.target == "rp2":
        skip_tests.add("thread/stress_heap.py")
        skip_tests.add("thread/thread_lock2.py")
        skip_tests.add("thread/thread_lock3.py")
        skip_tests.add("thread/thread_shared2.py")
    elif args.target == "zephyr":
        skip_tests.add("thread/stress_heap.py")
        skip_tests.add("thread/thread_lock3.py")

    # Some tests shouldn't be run on pyboard
    if args.target != "unix":
        skip_tests.add("basics/exception_chain.py")  # warning is not printed
        skip_tests.add("micropython/meminfo.py")  # output is very different to PC output

        if args.target == "wipy":
            skip_tests.add("misc/print_exception.py")  # requires error reporting full
            skip_tests.update(
                {
                    "extmod/uctypes_%s.py" % t
                    for t in "bytearray le native_le ptr_le ptr_native_le sizeof sizeof_native array_assign_le array_assign_native_le".split()
                }
            )  # requires uctypes
            skip_tests.add("extmod/heapq1.py")  # heapq not supported by WiPy
            skip_tests.add("extmod/random_basic.py")  # requires random
            skip_tests.add("extmod/random_extra.py")  # requires random
        elif args.target == "esp8266":
            skip_tests.add("micropython/viper_args.py")  # too large
            skip_tests.add("micropython/viper_binop_arith.py")  # too large
            skip_tests.add("misc/rge_sm.py")  # too large
        elif args.target == "minimal":
            skip_tests.add("basics/class_inplace_op.py")  # all special methods not supported
            skip_tests.add(
                "basics/subclass_native_init.py"
            )  # native subclassing corner cases not support
            skip_tests.add("misc/rge_sm.py")  # too large
            skip_tests.add("micropython/opt_level.py")  # don't assume line numbers are stored
        elif args.target == "nrf":
            skip_tests.add("basics/memoryview1.py")  # no item assignment for memoryview
            skip_tests.add("extmod/random_basic.py")  # unimplemented: random.seed
            skip_tests.add("micropython/opt_level.py")  # no support for line numbers
            skip_tests.add("misc/non_compliant.py")  # no item assignment for bytearray
            for t in tests:
                if t.startswith("basics/io_"):
                    skip_tests.add(t)
        elif args.target == "renesas-ra":
            skip_tests.add(
                "extmod/time_time_ns.py"
            )  # RA fsp rtc function doesn't support nano sec info
        elif args.target == "qemu":
            skip_tests.add("inlineasm/asmfpaddsub.py")  # requires Cortex-M4
            skip_tests.add("inlineasm/asmfpcmp.py")
            skip_tests.add("inlineasm/asmfpldrstr.py")
            skip_tests.add("inlineasm/asmfpmuldiv.py")
            skip_tests.add("inlineasm/asmfpsqrt.py")
        elif args.target == "webassembly":
            skip_tests.add("basics/string_format_modulo.py")  # can't print nulls to stdout
            skip_tests.add("basics/string_strip.py")  # can't print nulls to stdout
            skip_tests.add("extmod/asyncio_basic2.py")
            skip_tests.add("extmod/asyncio_cancel_self.py")
            skip_tests.add("extmod/asyncio_current_task.py")
            skip_tests.add("extmod/asyncio_exception.py")
            skip_tests.add("extmod/asyncio_gather_finished_early.py")
            skip_tests.add("extmod/asyncio_get_event_loop.py")
            skip_tests.add("extmod/asyncio_heaplock.py")
            skip_tests.add("extmod/asyncio_loop_stop.py")
            skip_tests.add("extmod/asyncio_new_event_loop.py")
            skip_tests.add("extmod/asyncio_threadsafeflag.py")
            skip_tests.add("extmod/asyncio_wait_for_fwd.py")
            skip_tests.add("extmod/binascii_a2b_base64.py")
            skip_tests.add("extmod/re_stack_overflow.py")
            skip_tests.add("extmod/time_res.py")
            skip_tests.add("extmod/vfs_posix.py")
            skip_tests.add("extmod/vfs_posix_enoent.py")
            skip_tests.add("extmod/vfs_posix_paths.py")
            skip_tests.add("extmod/vfs_userfs.py")
            skip_tests.add("micropython/emg_exc.py")
            skip_tests.add("micropython/extreme_exc.py")
            skip_tests.add("micropython/heapalloc_exc_compressed_emg_exc.py")

    # Some tests are known to fail on 64-bit machines
    if pyb is None and platform.architecture()[0] == "64bit":
        pass

    # Some tests use unsupported features on Windows
    if os.name == "nt":
        if not sysconfig.get_platform().startswith("mingw"):
            # Works but CPython uses '\' path separator
            skip_tests.add("import/import_file.py")

    # Some tests are known to fail with native emitter
    # Remove them from the below when they work
    if args.emit == "native":
        skip_tests.add("basics/gen_yield_from_close.py")  # require raise_varargs
        skip_tests.update(
            {"basics/async_%s.py" % t for t in "with with2 with_break with_return".split()}
        )  # require async_with
        skip_tests.update(
            {"basics/%s.py" % t for t in "try_reraise try_reraise2".split()}
        )  # require raise_varargs
        skip_tests.add("basics/annotate_var.py")  # requires checking for unbound local
        skip_tests.add("basics/del_deref.py")  # requires checking for unbound local
        skip_tests.add("basics/del_local.py")  # requires checking for unbound local
        skip_tests.add("basics/exception_chain.py")  # raise from is not supported
        skip_tests.add("basics/scope_implicit.py")  # requires checking for unbound local
        skip_tests.add("basics/sys_tracebacklimit.py")  # requires traceback info
        skip_tests.add("basics/try_finally_return2.py")  # requires raise_varargs
        skip_tests.add("basics/unboundlocal.py")  # requires checking for unbound local
        # CIRCUITPY-CHANGE
        skip_tests.update(
            (
                "basics/chained_exception.py",
                "circuitpython/traceback_test.py",
                "circuitpython/traceback_test_chained.py",
            )
        )  # because native doesn't have proper traceback info
        skip_tests.add("extmod/asyncio_event.py")  # unknown issue
        skip_tests.add("extmod/asyncio_lock.py")  # requires async with
        skip_tests.add("extmod/asyncio_micropython.py")  # unknown issue
        skip_tests.add("extmod/asyncio_wait_for.py")  # unknown issue
        skip_tests.add("misc/features.py")  # requires raise_varargs
        skip_tests.add(
            "misc/print_exception.py"
        )  # because native doesn't have proper traceback info
        skip_tests.add("misc/sys_exc_info.py")  # sys.exc_info() is not supported for native
        skip_tests.add("misc/sys_settrace_features.py")  # sys.settrace() not supported
        skip_tests.add("misc/sys_settrace_generator.py")  # sys.settrace() not supported
        skip_tests.add("misc/sys_settrace_loop.py")  # sys.settrace() not supported
        skip_tests.add(
            "micropython/emg_exc.py"
        )  # because native doesn't have proper traceback info
        skip_tests.add(
            "micropython/heapalloc_traceback.py"
        )  # because native doesn't have proper traceback info
        skip_tests.add(
            "micropython/opt_level_lineno.py"
        )  # native doesn't have proper traceback info
        skip_tests.add("micropython/schedule.py")  # native code doesn't check pending events
        skip_tests.add("stress/bytecode_limit.py")  # bytecode specific test

    def run_one_test(test_file):
        test_file = test_file.replace("\\", "/")
        test_file_abspath = os.path.abspath(test_file).replace("\\", "/")

        if args.filters:
            # Default verdict is the opposit of the first action
            verdict = "include" if args.filters[0][0] == "exclude" else "exclude"
            for action, pat in args.filters:
                if pat.search(test_file):
                    verdict = action
            if verdict == "exclude":
                return

        test_basename = test_file.replace("..", "_").replace("./", "").replace("/", "_")
        test_name = os.path.splitext(os.path.basename(test_file))[0]
        is_native = (
            test_name.startswith("native_")
            or test_name.startswith("viper_")
            or args.emit == "native"
        )
        is_endian = test_name.endswith("_endian")
        is_int_big = test_name.startswith("int_big") or test_name.endswith("_intbig")
        is_bytearray = test_name.startswith("bytearray") or test_name.endswith("_bytearray")
        is_set_type = test_name.startswith(("set_", "frozenset")) or test_name.endswith("_set")
        is_slice = test_name.find("slice") != -1 or test_name in misc_slice_tests
        is_async = test_name.startswith(("async_", "asyncio_"))
        is_const = test_name.startswith("const")
        is_io_module = test_name.startswith("io_")
        is_fstring = test_name.startswith("string_fstring")

        skip_it = test_file in skip_tests
        skip_it |= skip_native and is_native
        skip_it |= skip_endian and is_endian
        skip_it |= skip_int_big and is_int_big
        skip_it |= skip_bytearray and is_bytearray
        skip_it |= skip_set_type and is_set_type
        skip_it |= skip_slice and is_slice
        skip_it |= skip_async and is_async
        skip_it |= skip_const and is_const
        skip_it |= skip_revops and "reverse_op" in test_name
        skip_it |= skip_io_module and is_io_module
        skip_it |= skip_fstring and is_fstring

        if skip_it:
            print("skip ", test_file)
            skipped_tests.append(test_name)
            return

        # get expected output
        test_file_expected = test_file + ".exp"
        if os.path.isfile(test_file_expected):
            # expected output given by a file, so read that in
            with open(test_file_expected, "rb") as f:
                output_expected = f.read()
        else:
            # CIRCUITPY-CHANGE: set language & make sure testlib is available for `skip_ok`.
            e = {
                "PYTHONPATH": base_path("testlib"),
                "PATH": os.environ["PATH"],
                "LANG": "en_US.UTF-8",
            }
            # CIRCUITPY-CHANGE: --keep-path applies to PYTHONPATH as well
            if args.keep_path and os.getenv("PYTHONPATH"):
                e["PYTHONPATH"] += ":" + os.getenv("PYTHONPATH")

            # run CPython to work out expected output
            try:
                output_expected = subprocess.check_output(
                    CPYTHON3_CMD + [test_file_abspath],
                    cwd=os.path.dirname(test_file),
                    stderr=subprocess.STDOUT,
                    # CIRCUITPY-CHANGE: pass environment
                    env=e,
                )
            except subprocess.CalledProcessError:
                output_expected = b"CPYTHON3 CRASH"

        # canonical form for all host platforms is to use \n for end-of-line
        output_expected = output_expected.replace(b"\r\n", b"\n")

        # run MicroPython
        output_mupy = run_micropython(pyb, args, test_file, test_file_abspath)

        if output_mupy == b"SKIP\n":
            if pyb is not None and hasattr(pyb, "read_until"):
                # Running on a target over a serial connection, and the target requested
                # to skip the test.  It does this via a SystemExit which triggers a soft
                # reset.  Wait for the soft reset to finish, so we don't interrupt the
                # start-up code (eg boot.py) when preparing to run the next test.
                pyb.read_until(1, b"raw REPL; CTRL-B to exit\r\n")
            print("skip ", test_file)
            skipped_tests.append(test_name)
            return

        testcase_count.add(len(output_expected.splitlines()))

        filename_expected = os.path.join(result_dir, test_basename + ".exp")
        filename_mupy = os.path.join(result_dir, test_basename + ".out")

        if output_expected == output_mupy:
            print("pass ", test_file)
            passed_count.increment()
            rm_f(filename_expected)
            rm_f(filename_mupy)
        else:
            with open(filename_expected, "wb") as f:
                f.write(output_expected)
            with open(filename_mupy, "wb") as f:
                f.write(output_mupy)
            print("FAIL ", test_file)
            failed_tests.append((test_name, test_file))

        test_count.increment()

    if pyb:
        num_threads = 1

    if num_threads > 1:
        pool = ThreadPool(num_threads)
        pool.map(run_one_test, tests)
    else:
        for test in tests:
            run_one_test(test)

    print(
        "{} tests performed ({} individual testcases)".format(
            test_count.value, testcase_count.value
        )
    )
    print("{} tests passed".format(passed_count.value))

    skipped_tests = sorted(skipped_tests.value)
    if len(skipped_tests) > 0:
        print("{} tests skipped: {}".format(len(skipped_tests), " ".join(skipped_tests)))
    failed_tests = sorted(failed_tests.value)

    # Serialize regex added by append_filter.
    def to_json(obj):
        if isinstance(obj, re.Pattern):
            return obj.pattern
        return obj

    with open(os.path.join(result_dir, RESULTS_FILE), "w") as f:
        json.dump(
            {"args": vars(args), "failed_tests": [test[1] for test in failed_tests]},
            f,
            default=to_json,
        )

    if len(failed_tests) > 0:
        print(
            "{} tests failed: {}".format(
                len(failed_tests), " ".join(test[0] for test in failed_tests)
            )
        )
        return False

    # all tests succeeded
    return True


class append_filter(argparse.Action):
    def __init__(self, option_strings, dest, **kwargs):
        super().__init__(option_strings, dest, default=[], **kwargs)

    def __call__(self, parser, args, value, option):
        if not hasattr(args, self.dest):
            args.filters = []
        if option.startswith(("-e", "--e")):
            option = "exclude"
        else:
            option = "include"
        args.filters.append((option, re.compile(value)))


def main():
    cmd_parser = argparse.ArgumentParser(
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="""Run and manage tests for MicroPython.

Tests are discovered by scanning test directories for .py files or using the
specified test files. If test files nor directories are specified, the script
expects to be ran in the tests directory (where this file is located) and the
builtin tests suitable for the target platform are ran.
When running tests, run-tests.py compares the MicroPython output of the test with the output
produced by running the test through CPython unless a <test>.exp file is found, in which
case it is used as comparison.
If a test fails, run-tests.py produces a pair of <test>.out and <test>.exp files in the result
directory with the MicroPython output and the expectations, respectively.
""",
        epilog="""\
Options -i and -e can be multiple and processed in the order given. Regex
"search" (vs "match") operation is used. An action (include/exclude) of
the last matching regex is used:
  run-tests.py -i async - exclude all, then include tests containing "async" anywhere
  run-tests.py -e '/big.+int' - include all, then exclude by regex
  run-tests.py -e async -i async_foo - include all, exclude async, yet still include async_foo
""",
    )
    cmd_parser.add_argument("--target", default="unix", help="the target platform")
    cmd_parser.add_argument(
        "--device",
        default="/dev/ttyACM0",
        help="the serial device or the IP address of the pyboard",
    )
    cmd_parser.add_argument(
        "-b", "--baudrate", default=115200, help="the baud rate of the serial device"
    )
    cmd_parser.add_argument("-u", "--user", default="micro", help="the telnet login username")
    cmd_parser.add_argument("-p", "--password", default="python", help="the telnet login password")
    cmd_parser.add_argument(
        "-d", "--test-dirs", nargs="*", help="input test directories (if no files given)"
    )
    cmd_parser.add_argument(
        "-r", "--result-dir", default=base_path("results"), help="directory for test results"
    )
    cmd_parser.add_argument(
        "-e",
        "--exclude",
        action=append_filter,
        metavar="REGEX",
        dest="filters",
        help="exclude test by regex on path/name.py",
    )
    cmd_parser.add_argument(
        "-i",
        "--include",
        action=append_filter,
        metavar="REGEX",
        dest="filters",
        help="include test by regex on path/name.py",
    )
    cmd_parser.add_argument(
        "--emit", default="bytecode", help="MicroPython emitter to use (bytecode or native)"
    )
    cmd_parser.add_argument("--heapsize", help="heapsize to use (use default if not specified)")
    cmd_parser.add_argument(
        "--via-mpy", action="store_true", help="compile .py files to .mpy first"
    )
    cmd_parser.add_argument("--mpy-cross-flags", default="", help="flags to pass to mpy-cross")
    cmd_parser.add_argument(
        "--keep-path", action="store_true", help="do not clear MICROPYPATH when running tests"
    )
    cmd_parser.add_argument(
        "-j",
        "--jobs",
        default=multiprocessing.cpu_count(),
        metavar="N",
        type=int,
        help="Number of tests to run simultaneously",
    )
    cmd_parser.add_argument("files", nargs="*", help="input test files")
    cmd_parser.add_argument(
        "--print-failures",
        action="store_true",
        help="print the diff of expected vs. actual output for failed tests and exit",
    )
    cmd_parser.add_argument(
        "--clean-failures",
        action="store_true",
        help="delete the .exp and .out files from failed tests and exit",
    )
    cmd_parser.add_argument(
        "--run-failures",
        action="store_true",
        help="re-run only the failed tests",
    )
    args = cmd_parser.parse_args()

    if args.print_failures:
        for exp in glob(os.path.join(args.result_dir, "*.exp")):
            testbase = exp[:-4]
            print()
            print("FAILURE {0}".format(testbase))
            os.system("{0} {1}.exp {1}.out".format(DIFF, testbase))

        sys.exit(0)

    if args.clean_failures:
        for f in glob(os.path.join(args.result_dir, "*.exp")) + glob(
            os.path.join(args.result_dir, "*.out")
        ):
            os.remove(f)
        rm_f(os.path.join(args.result_dir, RESULTS_FILE))

        sys.exit(0)

    LOCAL_TARGETS = (
        "unix",
        "webassembly",
    )
    EXTERNAL_TARGETS = (
        "pyboard",
        "wipy",
        "esp8266",
        "esp32",
        "minimal",
        "nrf",
        "qemu",
        "renesas-ra",
        "rp2",
        "zephyr",
    )
    if args.target in LOCAL_TARGETS:
        pyb = None
        if args.target == "webassembly":
            pyb = PyboardNodeRunner()
    elif args.target in EXTERNAL_TARGETS:
        global pyboard
        sys.path.append(base_path("../tools"))
        import pyboard

        pyb = pyboard.Pyboard(args.device, args.baudrate, args.user, args.password)
        pyboard.Pyboard.run_script_on_remote_target = run_script_on_remote_target
        pyb.enter_raw_repl()
    else:
        raise ValueError("target must be one of %s" % ", ".join(LOCAL_TARGETS + EXTERNAL_TARGETS))

    # Automatically detect the native architecture for mpy-cross if not given.
    if not args.mpy_cross_flags:
        output = run_feature_check(pyb, args, "target_info.py")
        arch = str(output, "ascii").strip()
        if arch != "None":
            args.mpy_cross_flags = "-march=" + arch

    if args.run_failures and (any(args.files) or args.test_dirs is not None):
        raise ValueError(
            "--run-failures cannot be used together with files or --test-dirs arguments"
        )

    if args.run_failures:
        results_file = os.path.join(args.result_dir, RESULTS_FILE)
        if os.path.exists(results_file):
            with open(results_file, "r") as f:
                tests = json.load(f)["failed_tests"]
        else:
            tests = []
    elif len(args.files) == 0:
        test_extensions = ("*.py",)
        if args.target == "webassembly":
            test_extensions += ("*.js", "*.mjs")

        if args.test_dirs is None:
            test_dirs = (
                "basics",
                "circuitpython",  # CIRCUITPY-CHANGE
                "micropython",
                "misc",
                "extmod",
            )
            if args.target == "pyboard":
                # run pyboard tests
                test_dirs += ("float", "stress", "inlineasm", "ports/stm32")
            elif args.target in ("renesas-ra"):
                test_dirs += ("float", "inlineasm", "ports/renesas-ra")
            elif args.target == "rp2":
                test_dirs += ("float", "stress", "thread", "ports/rp2")
                if "arm" in args.mpy_cross_flags:
                    test_dirs += ("inlineasm",)
            elif args.target == "esp32":
                test_dirs += ("float", "stress", "thread")
            elif args.target in ("esp8266", "minimal", "nrf"):
                test_dirs += ("float",)
            elif args.target == "wipy":
                # run WiPy tests
                test_dirs += ("ports/cc3200",)
            elif args.target == "unix":
                # run PC tests
                test_dirs += (
                    "float",
                    "import",
                    "io",
                    "stress",
                    "unicode",
                    "cmdline",
                    "ports/unix",
                )
            elif args.target == "qemu":
                test_dirs += (
                    "float",
                    "inlineasm",
                    "ports/qemu",
                )
        else:
            # run tests from these directories
            test_dirs = args.test_dirs
        tests = sorted(
            test_file
            for test_files in (
                glob(os.path.join(dir, ext)) for dir in test_dirs for ext in test_extensions
            )
            for test_file in test_files
        )
    else:
        # tests explicitly given
        tests = args.files

    if not args.keep_path:
        # clear search path to make sure tests use only builtin modules and those that can be frozen
        # CIRCUITPY-CHANGE: Add testlib for skip_if and our async stuff.
        os.environ["MICROPYPATH"] = os.pathsep.join(
            [
                ".frozen",
                base_path("testlib"),
                base_path("../frozen/Adafruit_CircuitPython_asyncio"),
                base_path("../frozen/Adafruit_CircuitPython_Ticks"),
            ]
        )

    try:
        os.makedirs(args.result_dir, exist_ok=True)
        res = run_tests(pyb, tests, args, args.result_dir, args.jobs)
    finally:
        if pyb:
            pyb.close()

    if not res:
        sys.exit(1)


if __name__ == "__main__":
    main()
