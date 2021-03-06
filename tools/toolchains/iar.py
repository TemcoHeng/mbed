"""
mbed SDK
Copyright (c) 2011-2013 ARM Limited

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""
import re
from os import remove
from os.path import join, exists, dirname, splitext, exists

from tools.toolchains import mbedToolchain
from tools.settings import IAR_PATH
from tools.settings import GOANNA_PATH
from tools.hooks import hook_tool

class IAR(mbedToolchain):
    LIBRARY_EXT = '.a'
    LINKER_EXT = '.icf'
    STD_LIB_NAME = "%s.a"

    DIAGNOSTIC_PATTERN = re.compile('"(?P<file>[^"]+)",(?P<line>[\d]+)\s+(?P<severity>Warning|Error)(?P<message>.+)')

    DEFAULT_FLAGS = {
        'common': [
            "--no_wrap_diagnostics",
            # Pa050: No need to be notified about "non-native end of line sequence"
            # Pa084: Pointless integer comparison -> checks for the values of an enum, but we use values outside of the enum to notify errors (ie: NC).
            # Pa093: Implicit conversion from float to integer (ie: wait_ms(85.4) -> wait_ms(85))
            # Pa082: Operation involving two values from two registers (ie: (float)(*obj->MR)/(float)(LPC_PWM1->MR0))
            "-e", # Enable IAR language extension
            "--diag_suppress=Pa050,Pa084,Pa093,Pa082"],
        'asm': [],
        'c': [],
        'cxx': ["--guard_calls"],
        'ld': ["--skip_dynamic_initialization", "--threaded_lib"],
    }

    def __init__(self, target, options=None, notify=None, macros=None, silent=False, extra_verbose=False):
        mbedToolchain.__init__(self, target, options, notify, macros, silent, extra_verbose=extra_verbose)
        if target.core == "Cortex-M7F":
            cpuchoice = "Cortex-M7"
        else:
            cpuchoice = target.core
        # flags_cmd are used only by our scripts, the project files have them already defined,
        # using this flags results in the errors (duplication)
        # asm accepts --cpu Core or --fpu FPU, not like c/c++ --cpu=Core
        asm_flags_cmd = [
            "--cpu", cpuchoice
        ]
        # custom c flags
        c_flags_cmd = [
            "--cpu", cpuchoice,
            "--thumb", "--dlib_config", join(IAR_PATH, "inc", "c", "DLib_Config_Full.h")
        ]
        # custom c++ cmd flags
        cxx_flags_cmd = [
            "--c++", "--no_rtti", "--no_exceptions"
        ]
        if target.core == "Cortex-M7F":
            asm_flags_cmd += ["--fpu", "VFPv5_sp"]
            c_flags_cmd.append("--fpu=VFPv5_sp")

        if "debug-info" in self.options:
            c_flags_cmd.append("-r")
            c_flags_cmd.append("-On")
        else:
            c_flags_cmd.append("-Oh")

        IAR_BIN = join(IAR_PATH, "bin")
        main_cc = join(IAR_BIN, "iccarm")

        self.asm  = [join(IAR_BIN, "iasmarm")] + asm_flags_cmd + self.flags["asm"]
        if not "analyze" in self.options:
            self.cc   = [main_cc]
            self.cppc = [main_cc]
        else:
            self.cc   = [join(GOANNA_PATH, "goannacc"), '--with-cc="%s"' % main_cc.replace('\\', '/'), "--dialect=iar-arm", '--output-format="%s"' % self.GOANNA_FORMAT]
            self.cppc = [join(GOANNA_PATH, "goannac++"), '--with-cxx="%s"' % main_cc.replace('\\', '/'), "--dialect=iar-arm", '--output-format="%s"' % self.GOANNA_FORMAT]
        self.cc += self.flags["common"] + c_flags_cmd + self.flags["c"]
        self.cppc += self.flags["common"] + c_flags_cmd + cxx_flags_cmd + self.flags["cxx"]
        self.ld   = join(IAR_BIN, "ilinkarm")
        self.ar = join(IAR_BIN, "iarchive")
        self.elf2bin = join(IAR_BIN, "ielftool")

    def parse_dependencies(self, dep_path):
        return [path.strip() for path in open(dep_path).readlines()
                if (path and not path.isspace())]

    def parse_output(self, output):
        for line in output.splitlines():
            match = IAR.DIAGNOSTIC_PATTERN.match(line)
            if match is not None:
                self.cc_info(
                    match.group('severity').lower(),
                    match.group('file'),
                    match.group('line'),
                    match.group('message'),
                    target_name=self.target.name,
                    toolchain_name=self.name
                )
            match = self.goanna_parse_line(line)
            if match is not None:
                self.cc_info(
                    match.group('severity').lower(),
                    match.group('file'),
                    match.group('line'),
                    match.group('message')
                )

    def get_dep_option(self, object):
        base, _ = splitext(object)
        dep_path = base + '.d'
        return ["--dependencies", dep_path]

    def cc_extra(self, object):
        base, _ = splitext(object)
        return ["-l", base + '.s.txt']

    def get_compile_options(self, defines, includes):
        opts = ['-D%s' % d for d in defines] + ['-f', self.get_inc_file(includes)]
        config_header = self.get_config_header()
        if config_header is not None:
            opts = opts + ['--preinclude', config_header]
        return opts

    @hook_tool
    def assemble(self, source, object, includes):
        # Build assemble command
        cmd = self.asm + self.get_compile_options(self.get_symbols(), includes) + ["-o", object, source]

        # Call cmdline hook
        cmd = self.hook.get_cmdline_assembler(cmd)

        # Return command array, don't execute
        return [cmd]

    @hook_tool
    def compile(self, cc, source, object, includes):
        # Build compile command
        cmd = cc +  self.get_compile_options(self.get_symbols(), includes)

        cmd.extend(self.get_dep_option(object))

        cmd.extend(self.cc_extra(object))
        
        cmd.extend(["-o", object, source])

        # Call cmdline hook
        cmd = self.hook.get_cmdline_compiler(cmd)

        return [cmd]

    def compile_c(self, source, object, includes):
        return self.compile(self.cc, source, object, includes)

    def compile_cpp(self, source, object, includes):
        return self.compile(self.cppc, source, object, includes)

    @hook_tool
    def link(self, output, objects, libraries, lib_dirs, mem_map):
        # Build linker command
        map_file = splitext(output)[0] + ".map"
        cmd = [self.ld, "-o", output, "--map=%s" % map_file] + objects + libraries + self.flags['ld']

        if mem_map:
            cmd.extend(["--config", mem_map])

        # Call cmdline hook
        cmd = self.hook.get_cmdline_linker(cmd)

        # Split link command to linker executable + response file
        link_files = join(dirname(output), ".link_files.txt")
        with open(link_files, "wb") as f:
            cmd_linker = cmd[0]
            cmd_list = []
            for c in cmd[1:]:
                if c:
                    cmd_list.append(('"%s"' % c) if not c.startswith('-') else c)                    
            string = " ".join(cmd_list).replace("\\", "/")
            f.write(string)

        # Exec command
        self.default_cmd([cmd_linker, '-f', link_files])

    @hook_tool
    def archive(self, objects, lib_path):
        archive_files = join(dirname(lib_path), ".archive_files.txt")
        with open(archive_files, "wb") as f:
            o_list = []
            for o in objects:
                o_list.append('"%s"' % o)                    
            string = " ".join(o_list).replace("\\", "/")
            f.write(string)

        if exists(lib_path):
            remove(lib_path)

        self.default_cmd([self.ar, lib_path, '-f', archive_files])

    @hook_tool
    def binary(self, resources, elf, bin):
        # Build binary command
        cmd = [self.elf2bin, "--bin", elf, bin]

        # Call cmdline hook
        cmd = self.hook.get_cmdline_binary(cmd)

        # Exec command
        self.default_cmd(cmd)
