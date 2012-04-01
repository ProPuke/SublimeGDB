"""
Copyright (c) 2012 Fredrik Ehnbom

This software is provided 'as-is', without any express or implied
warranty. In no event will the authors be held liable for any damages
arising from the use of this software.

Permission is granted to anyone to use this software for any purpose,
including commercial applications, and to alter it and redistribute it
freely, subject to the following restrictions:

   1. The origin of this software must not be misrepresented; you must not
   claim that you wrote the original software. If you use this software
   in a product, an acknowledgment in the product documentation would be
   appreciated but is not required.

   2. Altered source versions must be plainly marked as such, and must not be
   misrepresented as being the original software.

   3. This notice may not be removed or altered from any source
   distribution.
"""
import sublime
import sublime_plugin
import subprocess
import struct
import threading
import time
import traceback
import os
import re
import Queue
from resultparser import parse_result_line
from types import ListType


def get_setting(key, default=None):
    try:
        s = sublime.active_window().active_view().settings()
        if s.has("sublimegdb_%s" % key):
            return s.get("sublimegdb_%s" % key)
    except:
        pass
    return sublime.load_settings("SublimeGDB.sublime-settings").get(key, default)

DEBUG = get_setting("debug", False)
DEBUG_FILE = get_setting("debug_file", "/tmp/sublimegdb.txt")

breakpoints = {}
gdb_lastresult = ""
gdb_lastline = ""
gdb_cursor = ""
gdb_cursor_position = 0
gdb_last_cursor_view = None
gdb_bkp_layout = {}
gdb_bkp_window = None
gdb_bkp_view = None

gdb_shutting_down = False
gdb_process = None
gdb_stack_frame = None
gdb_stack_index = 0

gdb_nonstop = True

if os.name == 'nt':
    gdb_nonstop = False


gdb_run_status = None
result_regex = re.compile("(?<=\^)[^,\"]*")


def log_debug(line):
    if DEBUG:
        os.system("echo \"%s\" >> \"%s\"" % (line, DEBUG_FILE))


class GDBView(object):
    def __init__(self, name, s=True, settingsprefix=None):
        self.queue = Queue.Queue()
        self.name = name
        self.closed = True
        self.doScroll = s
        self.view = None
        self.settingsprefix = settingsprefix

    def is_open(self):
        return not self.closed

    def open_at_start(self):
        if self.settingsprefix != None:
            return get_setting("%s_open" % self.settingsprefix, False)
        return False

    def open(self):
        if self.view == None or self.view.window() == None:
            if self.settingsprefix != None:
                sublime.active_window().focus_group(get_setting("%s_group" % self.settingsprefix, 0))
            self.create_view()

    def close(self):
        if self.view != None:
            if self.settingsprefix != None:
                sublime.active_window().focus_group(get_setting("%s_group" % self.settingsprefix, 0))
            self.destroy_view()

    def should_update(self):
        return self.is_open() and is_running() and gdb_run_status == "stopped"

    def set_syntax(self, syntax):
        if self.is_open():
            self.get_view().set_syntax_file(syntax)

    def add_line(self, line, now=False):
        if self.is_open():
            if not now:
                self.queue.put((self.do_add_line, line))
                sublime.set_timeout(self.update, 0)
            else:
                self.do_add_line(line)

    def scroll(self, line):
        if self.is_open():
            self.queue.put((self.do_scroll, line))
            sublime.set_timeout(self.update, 0)

    def set_viewport_position(self, pos):
        if self.is_open():
            self.queue.put((self.do_set_viewport_position, pos))
            sublime.set_timeout(self.update, 0)

    def clear(self, now=False):
        if self.is_open():
            if not now:
                self.queue.put((self.do_clear, None))
                sublime.set_timeout(self.update, 0)
            else:
                self.do_clear(None)

    def create_view(self):
        self.view = sublime.active_window().new_file()
        self.view.set_name(self.name)
        self.view.set_scratch(True)
        self.view.set_read_only(True)
        # Setting command_mode to false so that vintage
        # does not eat the "enter" keybinding
        self.view.settings().set('command_mode', False)
        self.closed = False

    def destroy_view(self):
        sublime.active_window().focus_view(self.view)
        sublime.active_window().run_command("close")

    def is_closed(self):
        return self.closed

    def was_closed(self):
        self.closed = True

    def fold_all(self):
        if self.is_open():
            self.queue.put((self.do_fold_all, None))

    def get_view(self):
        return self.view

    def do_add_line(self, line):
        self.view.set_read_only(False)
        e = self.view.begin_edit()
        self.view.insert(e, self.view.size(), line)
        self.view.end_edit(e)
        self.view.set_read_only(True)
        if self.doScroll:
            self.view.show(self.view.size())

    def do_fold_all(self, data):
        self.view.run_command("fold_all")

    def do_clear(self, data):
        self.view.set_read_only(False)
        e = self.view.begin_edit()
        self.view.erase(e, sublime.Region(0, self.view.size()))
        self.view.end_edit(e)
        self.view.set_read_only(True)

    def do_scroll(self, data):
        self.view.run_command("goto_line", {"line": data + 1})

    def do_set_viewport_position(self, data):
        # Shouldn't have to call viewport_extent, but it
        # seems to flush whatever value is stale so that
        # the following set_viewport_position works.
        # Keeping it around as a WAR until it's fixed
        # in Sublime Text 2.
        self.view.viewport_extent()
        self.view.set_viewport_position(data, False)

    def update(self):
        if not self.is_open():
            return
        try:
            while not self.queue.empty():
                cmd, data = self.queue.get()
                try:
                    cmd(data)
                finally:
                    self.queue.task_done()
        except:
            traceback.print_exc()


class GDBVariable:
    def __init__(self, vp=None):
        self.valuepair = vp
        self.children = []
        self.line = 0
        self.is_expanded = False
        if "value" not in vp:
            self.update_value()
        self.dirty = False
        self.deleted = False

    def delete(self):
        run_cmd("-var-delete %s" % self.get_name())
        self.deleted = True

    def update_value(self):
        line = run_cmd("-var-evaluate-expression %s" % self["name"], True)
        if get_result(line) == "done":
            self['value'] = parse_result_line(line)["value"]

    def update(self, d):
        for key in d:
            if key.startswith("new_"):
                if key == "new_num_children":
                    self["numchild"] = d[key]
                else:
                    self[key[4:]] = d[key]
            elif key == "value":
                self[key] = d[key]

    def add_children(self, name):
        children = listify(parse_result_line(run_cmd("-var-list-children 1 \"%s\"" % name, True))["children"]["child"])
        for child in children:
            child = GDBVariable(child)
            if child.get_name().endswith(".private") or \
                    child.get_name().endswith(".protected") or \
                    child.get_name().endswith(".public"):
                if child.has_children():
                    self.add_children(child.get_name())
            else:
                self.children.append(child)

    def is_editable(self):
        line = run_cmd("-var-show-attributes %s" % (self.get_name()), True)
        return "editable" in re.findall("(?<=attr=\")[a-z]+(?=\")", line)

    def edit_on_done(self, val):
        line = run_cmd("-var-assign %s \"%s\"" % (self.get_name(), val), True)
        if get_result(line) == "done":
            self.valuepair["value"] = parse_result_line(line)["value"]
            gdb_variables_view.update_variables(True)
        else:
            err = line[line.find("msg=") + 4:]
            sublime.status_message("Error: %s" % err)

    def find(self, name):
        if self.deleted:
            return None
        if name == self.get_name():
            return self
        elif name.startswith(self.get_name()):
            for child in self.children:
                ret = child.find(name)
                if ret != None:
                    return ret
        return None

    def edit(self):
        sublime.active_window().show_input_panel("%s =" % self["exp"], self.valuepair["value"], self.edit_on_done, None, None)

    def get_name(self):
        return self.valuepair["name"]

    def expand(self):
        self.is_expanded = True
        if not (len(self.children) == 0 and int(self.valuepair["numchild"]) > 0):
            return
        self.add_children(self.get_name())

    def has_children(self):
        return int(self.valuepair["numchild"]) > 0

    def collapse(self):
        self.is_expanded = False

    def __str__(self):
        if not "dynamic_type" in self or len(self['dynamic_type']) == 0 or self['dynamic_type'] == self['type']:
            return "%s %s = %s" % (self['type'], self['exp'], self['value'])
        else:
            return "%s %s = (%s) %s" % (self['type'], self['exp'], self['dynamic_type'], self['value'])

    def __iter__(self):
        return self.valuepair.__iter__()

    def __getitem__(self, key):
        return self.valuepair[key]

    def __setitem__(self, key, value):
        self.valuepair[key] = value
        if key == "value":
            self.dirty = True

    def clear_dirty(self):
        self.dirty = False
        for child in self.children:
            child.clear_dirty()

    def is_dirty(self):
        dirt = self.dirty
        if not dirt and not self.is_expanded:
            for child in self.children:
                if child.is_dirty():
                    dirt = True
                    break
        return dirt

    def format(self, indent="", output="", line=0, dirty=[]):
        icon = " "
        if self.has_children():
            if self.is_expanded:
                icon = "-"
            else:
                icon = "+"

        output += "%s%s%s\n" % (indent, icon, self)
        self.line = line
        line = line + 1
        indent += "    "
        if self.is_expanded:
            for child in self.children:
                output, line = child.format(indent, output, line, dirty)
        if self.is_dirty():
            dirty.append(self)
        return (output, line)


class GDBRegister:
    def __init__(self, name, index, val):
        self.name = name
        self.index = index
        self.value = val
        self.line = 0
        self.lines = 0

    def format(self, line=0):
        val = self.value
        if  "{" not in val:
            valh = int(val, 16)&0xffffffffffffffffffffffffffffffff
            six4 = False
            if valh > 0xffffffff:
                six4 = True
            val = struct.pack("Q" if six4 else "I", valh)
            valf = struct.unpack("d" if six4 else "f", val)[0]
            valI = struct.unpack("Q" if six4 else "I", val)[0]
            vali = struct.unpack("q" if six4 else "i", val)[0]

            val = "0x%016x %16.8f %020d %020d" % (valh, valf, valI, vali)
        output = "%8s: %s\n" % (self.name, val)
        self.line = line
        line += output.count("\n")
        self.lines = line - self.line
        return (output, line)

    def set_value(self, val):
        self.value = val

    def set_gdb_value(self, val):
        if "." in val:
            if val.endswith("f"):
                val = struct.unpack("I", struct.pack("f", float(val[:-1])))[0]
            else:
                val = struct.unpack("Q", struct.pack("d", float(val)))[0]

        run_cmd("-data-evaluate-expression $%s=%s" % (self.name, val))

    def edit_on_done(self, val):
        self.set_gdb_value(val)
        gdb_register_view.update_values()

    def edit(self):
        sublime.active_window().show_input_panel("$%s =" % self.name, self.value, self.edit_on_done, None, None)


class GDBRegisterView(GDBView):
    def __init__(self):
        super(GDBRegisterView, self).__init__("GDB Registers", s=False, settingsprefix="registers")
        self.values = None

    def open(self):
        super(GDBRegisterView, self).open()
        self.set_syntax("Packages/SublimeGDB/gdb_registers.tmLanguage")
        self.get_view().settings().set("word_wrap", False)
        if self.is_open() and gdb_run_status == "stopped":
            self.update_values()

    def get_names(self):
        line = run_cmd("-data-list-register-names", True)
        return parse_result_line(line)["register-names"]

    def get_values(self):
        line = run_cmd("-data-list-register-values x", True)
        if get_result(line) != "done":
            return []
        return parse_result_line(line)["register-values"]

    def update_values(self):
        if not self.should_update():
            return
        dirtylist = []
        if self.values == None:
            names = self.get_names()
            vals = self.get_values()
            self.values = []

            for i in range(len(vals)):
                idx = int(vals[i]["number"])
                self.values.append(GDBRegister(names[idx], idx, vals[i]["value"]))
        else:
            dirtylist = regs = parse_result_line(run_cmd("-data-list-changed-registers", True))["changed-registers"]
            regvals = parse_result_line(run_cmd("-data-list-register-values x %s" % " ".join(regs), True))["register-values"]
            for i in range(len(regs)):
                reg = int(regvals[i]["number"])
                if reg < len(self.values):
                    self.values[reg].set_value(regvals[i]["value"])
        self.clear()
        line = 0
        for item in self.values:
            output, line = item.format(line)
            self.add_line(output)
        self.update()
        regions = []
        v = self.get_view()
        for dirty in dirtylist:
            i = int(dirty)
            if i >= len(self.values):
                continue
            region = v.full_line(v.text_point(self.values[i].line, 0))
            if self.values[i].lines > 1:
                region = region.cover(v.full_line(v.text_point(self.values[i].line + self.values[i].lines - 1, 0)))

            regions.append(region)
        v.add_regions("sublimegdb.dirtyregisters", regions,
                        get_setting("changed_variable_scope", "entity.name.class"),
                        get_setting("changed_variable_icon", ""),
                        sublime.DRAW_OUTLINED)

    def get_register_at_line(self, line):
        if self.values == None:
            return None
        for i in range(len(self.values)):
            if self.values[i].line == line:
                return self.values[i]
            elif self.values[i].line > line:
                return self.values[i - 1]
        return None


class GDBVariablesView(GDBView):
    def __init__(self):
        super(GDBVariablesView, self).__init__("GDB Variables", False, settingsprefix="variables")
        self.variables = []

    def open(self):
        super(GDBVariablesView, self).open()
        self.set_syntax("Packages/C++/C++.tmLanguage")
        if self.is_open() and gdb_run_status == "stopped":
            self.update_variables(False)

    def update_view(self):
        self.clear()
        output = ""
        line = 0
        dirtylist = []
        for local in self.variables:
            output, line = local.format(line=line, dirty=dirtylist)
            self.add_line(output)
        self.update()
        regions = []
        v = self.get_view()
        for dirty in dirtylist:
            regions.append(v.full_line(v.text_point(dirty.line, 0)))
        v.add_regions("sublimegdb.dirtyvariables", regions,
                        get_setting("changed_variable_scope", "entity.name.class"),
                        get_setting("changed_variable_icon", ""),
                        sublime.DRAW_OUTLINED)

    def extract_varnames(self, res):
        if "name" in res:
            return listify(res["name"])
        elif len(res) > 0 and type(res) is ListType:
            if "name" in res[0]:
                return [x["name"] for x in res]
        return []

    def create_variable(self, exp):
        line = run_cmd("-var-create - * %s" % exp, True)
        var = parse_result_line(line)
        var['exp'] = exp
        return GDBVariable(var)

    def update_variables(self, sameFrame):
        if not self.should_update():
            return
        if sameFrame:
            for var in self.variables:
                var.clear_dirty()
            ret = parse_result_line(run_cmd("-var-update --all-values *", True))["changelist"]
            if "varobj" in ret:
                ret = listify(ret["varobj"])
            dellist = []
            for value in ret:
                name = value["name"]
                for var in self.variables:
                    real = var.find(name)
                    if real != None:
                        if  "in_scope" in value and value["in_scope"] == "false":
                            real.delete()
                            dellist.append(real)
                            continue
                        real.update(value)
                        if not "value" in value and not "new_value" in value:
                            real.update_value()
                        break
            for item in dellist:
                self.variables.remove(item)

            loc = self.extract_varnames(parse_result_line(run_cmd("-stack-list-locals 0", True))["locals"])
            tracked = []
            for var in loc:
                create = True
                for var2 in self.variables:
                    if var2['exp'] == var and var2 not in tracked:
                        tracked.append(var2)
                        create = False
                        break
                if create:
                    self.variables.append(self.create_variable(var))
        else:
            for var in self.variables:
                var.delete()
            args = self.extract_varnames(parse_result_line(run_cmd("-stack-list-arguments 0 %d %d" % (gdb_stack_index, gdb_stack_index), True))["stack-args"]["frame"]["args"])
            self.variables = []
            for arg in args:
                self.variables.append(self.create_variable(arg))
            loc = self.extract_varnames(parse_result_line(run_cmd("-stack-list-locals 0", True))["locals"])
            for var in loc:
                self.variables.append(self.create_variable(var))
        self.update_view()

    def get_variable_at_line(self, line, var_list=None):
        if var_list == None:
            var_list = self.variables
        if len(var_list) == 0:
            return None

        for i in range(len(var_list)):
            if var_list[i].line == line:
                return var_list[i]
            elif var_list[i].line > line:
                return self.get_variable_at_line(line, var_list[i - 1].children)
        return self.get_variable_at_line(line, var_list[len(var_list) - 1].children)

    def expand_collapse_variable(self, view, expand=True, toggle=False):
        row, col = view.rowcol(view.sel()[0].a)
        if self.is_open() and view.id() == self.get_view().id():
            var = self.get_variable_at_line(row)
            if var and var.has_children():
                if toggle:
                    if var.is_expanded:
                        var.collapse()
                    else:
                        var.expand()
                elif expand:
                    var.expand()
                else:
                    var.collapse()
                pos = view.viewport_position()
                self.update_view()
                self.set_viewport_position(pos)
                self.update()


class GDBCallstackFrame:
    def __init__(self, func, args):
        self.func = func
        self.args = args
        self.lines = 0

    def format(self):
        output = "%s(" % self.func
        for arg in self.args:
            if "name" in arg:
                output += arg["name"]
            if "value" in arg:
                output += " = %s" % arg["value"]
            output += ","
        output += ");\n"
        self.lines = output.count("\n")
        return output


class GDBCallstackView(GDBView):
    def __init__(self):
        super(GDBCallstackView, self).__init__("GDB Callstack", settingsprefix="callstack")
        self.frames = []

    def open(self):
        super(GDBCallstackView, self).open()
        self.set_syntax("Packages/C++/C++.tmLanguage")
        if self.is_open() and gdb_run_status == "stopped":
            self.update_callstack()

    def update_callstack(self):
        if not self.should_update():
            return
        global gdb_cursor_position
        line = run_cmd("-stack-list-frames", True)
        if get_result(line) == "error":
            gdb_cursor_position = 0
            update_view_markers()
            return
        frames = listify(parse_result_line(line)["stack"]["frame"])
        args = listify(parse_result_line(run_cmd("-stack-list-arguments 1", True))["stack-args"]["frame"])
        self.clear()

        self.frames = []
        for i in range(len(frames)):
            arg = {}
            if len(args) > i:
                arg = args[i]["args"]
            f = GDBCallstackFrame(frames[i]["func"], arg)
            self.frames.append(f)
            self.add_line(f.format())
        self.update()

    def update_marker(self, pos_scope, pos_icon):
        if self.is_open():
            view = self.get_view()
            if gdb_stack_index != -1:
                line = 0
                for i in range(gdb_stack_index):
                    line += self.frames[i].lines

                view.add_regions("sublimegdb.stackframe",
                                    [view.line(view.text_point(line, 0))],
                                    pos_scope, pos_icon, sublime.HIDDEN)
            else:
                view.erase_regions("sublimegdb.stackframe")

    def select(self, row):
        line = 0
        for i in range(len(self.frames)):
            fl = self.frames[i].lines
            if row <= line + fl - 1:
                run_cmd("-stack-select-frame %d" % i)
                update_cursor()
                break
            line += fl


class GDBThread:
    def __init__(self, id, state="UNKNOWN", func="???()"):
        self.id = id
        self.state = state
        self.func = func

    def format(self):
        return "%03d - %10s - %s\n" % (self.id, self.state, self.func)


class GDBThreadsView(GDBView):
    def __init__(self):
        super(GDBThreadsView, self).__init__("GDB Threads", s=False, settingsprefix="threads")
        self.threads = []
        self.current_thread = 0

    def open(self):
        super(GDBThreadsView, self).open()
        self.set_syntax("Packages/C++/C++.tmLanguage")
        if self.is_open() and gdb_run_status == "stopped":
            self.update_threads()

    def update_threads(self):
        if not self.should_update():
            return
        res = run_cmd("-thread-info", True)
        ids = parse_result_line(run_cmd("-thread-list-ids", True))
        if get_result(res) == "error":
            if "thread-ids" in ids and "thread-id" in ids["thread-ids"]:
                self.threads = [GDBThread(int(id)) for id in ids["thread-ids"]["thread-id"]]
                if "threads" in ids and "thread" in ids["threads"]:
                    for thread in ids["threads"]["thread"]:
                        if "thread-id" in thread and "state" in thread:
                            tid = int(thread["thread-id"])
                            for t2 in self.threads:
                                if t2.id == tid:
                                    t2.state = thread["state"]
                                    break
                else:
                    l = parse_result_line(run_cmd("-thread-info", True))
            else:
                self.threads = []
        else:
            l = parse_result_line(res)
            self.threads = []
            for thread in l["threads"]:
                func = "???"
                if "frame" in thread and "func" in thread["frame"]:
                    func = thread["frame"]["func"]
                    args = ""
                    if "args" in thread["frame"]:
                        for arg in thread["frame"]["args"]:
                            if len(args) > 0:
                                args += ", "
                            if "name" in arg:
                                args += arg["name"]
                            if "value" in arg:
                                args += " = " + arg["value"]
                    func = "%s(%s);" % (func, args)
                self.threads.append(GDBThread(int(thread["id"]), thread["state"], func))

        if "current-thread-id" in ids:
            self.current_thread = int(ids["current-thread-id"])
        self.clear(True)
        self.threads.sort(key=lambda t: t.id)
        for thread in self.threads:
            self.add_line(thread.format(), True)

    def update_marker(self, pos_scope, pos_icon):
        if self.is_open():
            view = self.get_view()
            line = -1
            for i in range(len(self.threads)):
                if self.threads[i].id == self.current_thread:
                    line = i
                    break

            if line != -1:
                view.add_regions("sublimegdb.currentthread",
                                    [view.line(view.text_point(line, 0))],
                                    pos_scope, pos_icon, sublime.HIDDEN)
            else:
                view.erase_regions("sublimegdb.currentthread")

    def select_thread(self, thread):
        run_cmd("-thread-select %d" % thread)
        self.current_thread = thread

    def select(self, row):
        if row >= len(self.threads):
            return
        self.select_thread(self.threads[row].id)


class GDBDisassemblyView(GDBView):
    def __init__(self):
        super(GDBDisassemblyView, self).__init__("GDB Disassembly", s=False, settingsprefix="disassembly")
        self.start = -1
        self.end = -1

    def open(self):
        super(GDBDisassemblyView, self).open()
        self.set_syntax("Packages/SublimeGDB/gdb_disasm.tmLanguage")
        self.get_view().settings().set("word_wrap", False)
        if self.is_open() and gdb_run_status == "stopped":
            self.update_disassembly()

    def clear(self):
        super(GDBDisassemblyView, self).clear()
        self.start = -1
        self.end = -1

    def add_insns(self, src_asm):
        for asm in src_asm:
            line = "%s: %s" % (asm["address"], asm["inst"])
            self.add_line("%-80s # %s+%s\n" % (line, asm["func-name"], asm["offset"]))
            addr = int(asm["address"], 16)
            if self.start == -1 or addr < self.start:
                self.start = addr
            self.end = addr

    def update_disassembly(self):
        if not self.should_update():
            return
        pc = parse_result_line(run_cmd("-data-evaluate-expression $pc", True))["value"]
        if " " in pc:
            pc = pc[:pc.find(" ")]
        pc = int(pc, 16)
        if not (pc >= self.start and pc <= self.end):
            l = run_cmd("-data-disassemble -s $pc -e \"$pc+200\" -- 1", True)
            asms = parse_result_line(l)
            self.clear()
            asms = asms["asm_insns"]
            if "src_and_asm_line" in asms:
                l = listify(asms["src_and_asm_line"])
                for src_asm in l:
                    line = src_asm["line"]
                    file = src_asm["file"]
                    self.add_line("%s:%s\n" % (file, line))
                    self.add_insns(src_asm["line_asm_insn"])
            else:
                self.add_insns(asms)
            self.update()
        view = self.get_view()
        reg = view.find("^0x[0]*%x:" % pc, 0)
        if reg is None:
            view.erase_regions("sublimegdb.programcounter")
        else:
            pos_scope = get_setting("position_scope", "entity.name.class")
            pos_icon = get_setting("position_icon", "bookmark")
            view.add_regions("sublimegdb.programcounter",
                            [reg],
                            pos_scope, pos_icon, sublime.HIDDEN)


gdb_session_view = GDBView("GDB Session", settingsprefix="session")
gdb_console_view = GDBView("GDB Console", settingsprefix="console")
gdb_variables_view = GDBVariablesView()
gdb_callstack_view = GDBCallstackView()
gdb_register_view = GDBRegisterView()
gdb_disassembly_view = GDBDisassemblyView()
gdb_threads_view = GDBThreadsView()
gdb_views = [gdb_session_view, gdb_console_view, gdb_variables_view, gdb_callstack_view, gdb_register_view, gdb_disassembly_view, gdb_threads_view]


def extract_breakpoints(line):
    res = parse_result_line(line)
    if "bkpt" in res["BreakpointTable"]:
        return listify(res["BreakpointTable"]["bkpt"])
    else:
        return listify(res["BreakpointTable"]["body"]["bkpt"])


def update_view_markers(view=None):
    if view == None:
        view = sublime.active_window().active_view()
    bps = []
    fn = view.file_name()
    if fn in breakpoints:
        for line in breakpoints[fn]:
            if not (line == gdb_cursor_position and fn == gdb_cursor):
                bps.append(view.full_line(view.text_point(line - 1, 0)))
    view.add_regions("sublimegdb.breakpoints", bps,
                        get_setting("breakpoint_scope", "keyword.gdb"),
                        get_setting("breakpoint_icon", "circle"),
                        sublime.HIDDEN)

    pos_scope = get_setting("position_scope", "entity.name.class")
    pos_icon = get_setting("position_icon", "bookmark")

    cursor = []
    if fn == gdb_cursor and gdb_cursor_position != 0:
        cursor.append(view.full_line(view.text_point(gdb_cursor_position - 1, 0)))
    global gdb_last_cursor_view
    if not gdb_last_cursor_view is None:
        gdb_last_cursor_view.erase_regions("sublimegdb.position")
    gdb_last_cursor_view = view
    view.add_regions("sublimegdb.position", cursor, pos_scope, pos_icon, sublime.HIDDEN)

    gdb_callstack_view.update_marker(pos_scope, pos_icon)
    gdb_threads_view.update_marker(pos_scope, pos_icon)

count = 0


def run_cmd(cmd, block=False, mimode=True):
    global count
    if not is_running():
        return "0^error,msg=\"no session running\""

    if mimode:
        count = count + 1
        cmd = "%d%s\n" % (count, cmd)
    else:
        cmd = "%s\n\n" % cmd
    log_debug(cmd)
    if gdb_session_view != None:
        gdb_session_view.add_line(cmd)
    gdb_process.stdin.write(cmd)
    if block:
        countstr = "%d^" % count
        i = 0
        while not gdb_lastresult.startswith(countstr) and i < 10000:
            i += 1
            time.sleep(0.001)
        if i >= 10000:
            raise ValueError("Command \"%s\" took longer than 10 seconds to perform?" % cmd)
        return gdb_lastresult
    return count


def wait_until_stopped():
    if gdb_run_status == "running":
        result = run_cmd("-exec-interrupt --all", True)
        if "^done" in result:
            i = 0
            while not "stopped" in gdb_run_status and i < 100:
                i = i + 1
                time.sleep(0.1)
            if i >= 100:
                print "I'm confused... I think status is %s, but it seems it wasn't..." % gdb_run_status
                return False
            return True
    return False


def resume():
    global gdb_run_status
    gdb_run_status = "running"
    run_cmd("-exec-continue", True)


def insert_breakpoint(filename, line):
    # Attempt to simplify file paths for windows. As some versions of gdb choke on drive specifiers
    if os.name == 'nt':
        filename = os.path.relpath(filename, get_setting('sourcedir'))
        filename = "'%s'" % filename

    cmd = "-break-insert \"%s:%d\"" % (filename, line)
    out = run_cmd(cmd, True)
    if get_result(out) == "error":
        return None, 0
    res = parse_result_line(out)
    if "bkpt" not in res and "matches" in res:
        cmd = "-break-insert *%s" % res["matches"]["b"][0]["addr"]
        out = run_cmd(cmd, True)
        if get_result(out) == "error":
            return None, 0
        res = parse_result_line(out)
    if "bkpt" not in res:
        return None, 0
    bp = res["bkpt"]
    f = bp["fullname"] if "fullname" in bp else bp["file"]
    return f, int(bp["line"])


def add_breakpoint(filename, line):
    if is_running():
        res = wait_until_stopped()
        f, line = insert_breakpoint(filename, line)
        if res:
            resume()
    breakpoints[filename].append(line)


def remove_breakpoint(filename, line):
    breakpoints[filename].remove(line)
    if is_running():
        res = wait_until_stopped()
        gdb_breakpoints = extract_breakpoints(run_cmd("-break-list", True))
        for bp in gdb_breakpoints:
            fn = bp["fullname"] if "fullname" in bp else bp["file"]
            if fn == filename and bp["line"] == str(line):
                run_cmd("-break-delete %s" % bp["number"])
                break
        if res:
            resume()


def toggle_breakpoint(filename, line):
    if line in breakpoints[filename]:
        remove_breakpoint(filename, line)
    else:
        add_breakpoint(filename, line)


def sync_breakpoints():
    global breakpoints
    newbps = {}
    for file in breakpoints:
        for bp in breakpoints[file]:
            if file in newbps:
                if bp in newbps[file]:
                    continue
            f, line = insert_breakpoint(file, bp)
            if f == None:
                continue
            if not f in newbps:
                newbps[f] = []
            newbps[f].append(line)
    breakpoints = newbps
    update_view_markers()


def get_result(line):
    return result_regex.search(line).group(0)


def listify(var):
    if not type(var) is ListType:
        return [var]
    return var


def update_cursor():
    global gdb_cursor
    global gdb_cursor_position
    global gdb_stack_index
    global gdb_stack_frame

    res = run_cmd("-stack-info-frame", True)
    if get_result(res) == "error":
        if gdb_run_status != "running":
            print "run_status is %s, but got error: %s" % (gdb_run_status, res)
        return
    currFrame = parse_result_line(res)["frame"]
    gdb_stack_index = int(currFrame["level"])

    if "fullname" in currFrame:
        gdb_cursor = currFrame["fullname"]
        gdb_cursor_position = int(currFrame["line"])
        sublime.active_window().focus_group(get_setting("file_group", 0))
        sublime.active_window().open_file("%s:%d" % (gdb_cursor, gdb_cursor_position), sublime.ENCODED_POSITION)
    else:
        gdb_cursor_position = 0

    sameFrame = gdb_stack_frame != None and \
                gdb_stack_frame["func"] == currFrame["func"]
    if sameFrame and "shlibname" in currFrame and "shlibname" in gdb_stack_frame:
        sameFrame = currFrame["shlibname"] == gdb_stack_frame["shlibname"]
    if sameFrame and "fullname" in currFrame and "fullname" in gdb_stack_frame:
        sameFrame = currFrame["fullname"] == gdb_stack_frame["fullname"]

    gdb_stack_frame = currFrame
    # Always need to update the callstack since it's possible to
    # end up in the current function from many different call stacks
    gdb_callstack_view.update_callstack()
    gdb_threads_view.update_threads()

    update_view_markers()
    gdb_variables_view.update_variables(sameFrame)
    gdb_register_view.update_values()
    gdb_disassembly_view.update_disassembly()


def session_ended_status_message():
    sublime.status_message("GDB session ended")


def gdboutput(pipe):
    global gdb_process
    global gdb_lastresult
    global gdb_lastline
    global gdb_stack_frame
    global gdb_run_status
    global gdb_stack_index
    command_result_regex = re.compile("^\d+\^")
    run_status_regex = re.compile("(^\d*\*)([^,]+)")
    while True:
        try:
            if gdb_process.poll() != None:
                break
            line = pipe.readline().strip()

            if len(line) > 0:
                log_debug(line)
                gdb_session_view.add_line("%s\n" % line)

                run_status = run_status_regex.match(line)
                if run_status != None:
                    gdb_run_status = run_status.group(2)
                    reason = re.search("(?<=reason=\")[a-zA-Z0-9\-]+(?=\")", line)
                    if reason != None and reason.group(0).startswith("exited"):
                        run_cmd("-gdb-exit")
                    elif not "running" in gdb_run_status and not gdb_shutting_down:
                        thread_id = re.search('thread-id="(\d+)"', line)
                        if thread_id != None:
                            gdb_threads_view.select_thread(int(thread_id.group(1)))
                        sublime.set_timeout(update_cursor, 0)
                if not line.startswith("(gdb)"):
                    gdb_lastline = line
                if command_result_regex.match(line) != None:
                    gdb_lastresult = line

                if line.startswith("~"):
                    gdb_console_view.add_line(
                        line[2:-1].replace("\\n", "\n").replace("\\\"", "\"").replace("\\t", "\t"))

        except:
            traceback.print_exc()
    if pipe == gdb_process.stdout:
        gdb_session_view.add_line("GDB session ended\n")
        sublime.set_timeout(session_ended_status_message, 0)
        gdb_stack_frame = None
    global gdb_cursor_position
    gdb_stack_index = -1
    gdb_cursor_position = 0
    gdb_run_status = None
    sublime.set_timeout(update_view_markers, 0)
    gdb_callstack_view.clear()
    gdb_register_view.clear()
    gdb_disassembly_view.clear()
    gdb_variables_view.clear()
    gdb_threads_view.clear()
    sublime.set_timeout(cleanup, 0)


def cleanup():
    if get_setting("close_views", True):
        for view in gdb_views:
            view.close()
    if get_setting("push_pop_layout", True):
        gdb_bkp_window.set_layout(gdb_bkp_layout)
        gdb_bkp_window.focus_view(gdb_bkp_view)


def programoutput():
    global gdb_process
    pipe = None
    while True:
        try:
            proc = gdb_process.poll() != None
            if pipe == None:
                try:
                    pipe = open("/tmp/sublimegdb_output.txt", "r")
                except:
                    pass
                if pipe == None:
                    if proc:
                        break
                    time.sleep(1.0)
                    continue

            line = pipe.readline()
            if len(line) > 0:
                gdb_console_view.add_line(line)
            else:
                if proc:
                    break
                time.sleep(0.1)
        except:
            traceback.print_exc()
    if not pipe == None:
        pipe.close()


def show_input():
    sublime.active_window().show_input_panel("GDB", "", input_on_done, input_on_change, input_on_cancel)


def input_on_done(s):
    run_cmd(s)
    if s.strip() != "quit":
        show_input()


def input_on_cancel():
    pass


def input_on_change(s):
    pass


def is_running():
    return gdb_process != None and gdb_process.poll() == None


class GdbInput(sublime_plugin.WindowCommand):
    def run(self):
        show_input()


class GdbLaunch(sublime_plugin.WindowCommand):
    def run(self):
        global gdb_process
        global gdb_run_status
        global gdb_bkp_window
        global gdb_bkp_view
        global gdb_bkp_layout
        global gdb_shutting_down
        if gdb_process == None or gdb_process.poll() != None:
            executable = get_setting("executable")
            gdb_process = subprocess.Popen(["gdb","--interpreter=mi",executable], shell=True, cwd=get_setting("workingdir", "/tmp"),
                                            stdin=subprocess.PIPE, stdout=subprocess.PIPE)

            gdb_bkp_window = sublime.active_window()
            #back up current layout before opening the debug one
            #it will be restored when debog is finished
            gdb_bkp_layout = gdb_bkp_window.get_layout()
            gdb_bkp_view = gdb_bkp_window.active_view()
            gdb_bkp_window.set_layout(
                get_setting("layout",
                    {
                        "cols": [0.0, 0.5, 1.0],
                        "rows": [0.0, 0.75, 1.0],
                        "cells": [[0, 0, 2, 1], [0, 1, 1, 2], [1, 1, 2, 2]]
                    }
                )
            )

            for view in gdb_views:
                if view.is_closed() and view.open_at_start():
                    view.open()
                view.clear()

            gdb_shutting_down = False

            t = threading.Thread(target=gdboutput, args=(gdb_process.stdout,))
            t.start()
            f = open("/tmp/sublimegdb_output.txt", "w")
            f.close()
            t = threading.Thread(target=programoutput)
            t.start()
            try:
                run_cmd("-gdb-show interpreter", True)
            except:
                sublime.error_message("""\
It seems you're not running gdb with the "mi" interpreter. Please add
"--interpreter=mi" to your gdb command line""")
                gdb_process.stdin.write("quit\n")
                return
            run_cmd("-inferior-tty-set /tmp/sublimegdb_output.txt")

            run_cmd("-gdb-set target-async 1")
            run_cmd("-gdb-set pagination off")
            if gdb_nonstop:
                run_cmd("-gdb-set non-stop on")

            sync_breakpoints()
            gdb_run_status = "running"
            run_cmd(get_setting("exec_cmd"), "-exec-run", True)

            show_input()
        else:
            sublime.status_message("GDB is already running!")

    def is_enabled(self):
        return not is_running()

    def is_visible(self):
        return not is_running()


class GdbContinue(sublime_plugin.WindowCommand):
    def run(self):
        global gdb_cursor_position
        gdb_cursor_position = 0
        update_view_markers()
        resume()

    def is_enabled(self):
        return is_running() and gdb_run_status != "running"

    def is_visible(self):
        return is_running()


class GdbExit(sublime_plugin.WindowCommand):
    def run(self):
        global gdb_shutting_down
        gdb_shutting_down = True
        wait_until_stopped()
        run_cmd("-gdb-exit", True)

    def is_enabled(self):
        return is_running()

    def is_visible(self):
        return is_running()


class GdbPause(sublime_plugin.WindowCommand):
    def run(self):
        run_cmd("-exec-interrupt")

    def is_enabled(self):
        return is_running() and gdb_run_status != "stopped"

    def is_visible(self):
        return is_running() and gdb_run_status != "stopped"


class GdbStepOver(sublime_plugin.WindowCommand):
    def run(self):
        run_cmd("-exec-next")

    def is_enabled(self):
        return is_running() and gdb_run_status != "running"

    def is_visible(self):
        return is_running()


class GdbStepInto(sublime_plugin.WindowCommand):
    def run(self):
        run_cmd("-exec-step")

    def is_enabled(self):
        return is_running() and gdb_run_status != "running"

    def is_visible(self):
        return is_running()


class GdbNextInstruction(sublime_plugin.WindowCommand):
    def run(self):
        run_cmd("-exec-next-instruction")

    def is_enabled(self):
        return is_running() and gdb_run_status != "running"

    def is_visible(self):
        return is_running()


class GdbStepOut(sublime_plugin.WindowCommand):
    def run(self):
        run_cmd("-exec-finish")

    def is_enabled(self):
        return is_running() and gdb_run_status != "running"

    def is_visible(self):
        return is_running()


class GdbToggleBreakpoint(sublime_plugin.TextCommand):
    def run(self, edit):
        fn = self.view.file_name()
        if fn not in breakpoints:
            breakpoints[fn] = []

        for sel in self.view.sel():
            line, col = self.view.rowcol(sel.a)
            toggle_breakpoint(fn, line + 1)
        update_view_markers(self.view)


class GdbClick(sublime_plugin.TextCommand):
    def run(self, edit):
        if not is_running():
            return

        row, col = self.view.rowcol(self.view.sel()[0].a)
        if gdb_variables_view.is_open() and self.view.id() == gdb_variables_view.get_view().id():
            gdb_variables_view.expand_collapse_variable(self.view, toggle=True)
        elif gdb_callstack_view.is_open() and self.view.id() == gdb_callstack_view.get_view().id():
            gdb_callstack_view.select(row)
        elif gdb_threads_view.is_open() and self.view.id() == gdb_threads_view.get_view().id():
            gdb_threads_view.select(row)
            update_cursor()

    def is_enabled(self):
        return is_running()


class GdbDoubleClick(sublime_plugin.TextCommand):
    def run(self, edit):
        if gdb_variables_view.is_open() and self.view.id() == gdb_variables_view.get_view().id():
            self.view.run_command("gdb_edit_variable")
        else:
            self.view.run_command("gdb_edit_register")

    def is_enabled(self):
        return is_running() and \
                ((gdb_variables_view.is_open() and self.view.id() == gdb_variables_view.get_view().id()) or \
                 (gdb_register_view.is_open() and self.view.id() == gdb_register_view.get_view().id()))


class GdbCollapseVariable(sublime_plugin.TextCommand):
    def run(self, edit):
        gdb_variables_view.expand_collapse_variable(self.view, expand=False)

    def is_enabled(self):
        if not is_running():
            return False
        row, col = self.view.rowcol(self.view.sel()[0].a)
        if gdb_variables_view.is_open() and self.view.id() == gdb_variables_view.get_view().id():
            return True
        return False


class GdbExpandVariable(sublime_plugin.TextCommand):
    def run(self, edit):
        gdb_variables_view.expand_collapse_variable(self.view)

    def is_enabled(self):
        if not is_running():
            return False
        row, col = self.view.rowcol(self.view.sel()[0].a)
        if gdb_variables_view.is_open() and self.view.id() == gdb_variables_view.get_view().id():
            return True
        return False


class GdbEditVariable(sublime_plugin.TextCommand):
    def run(self, edit):
        row, col = self.view.rowcol(self.view.sel()[0].a)
        var = gdb_variables_view.get_variable_at_line(row)
        if var.is_editable():
            var.edit()
        else:
            sublime.status_message("Variable isn't editable")

    def is_enabled(self):
        if not is_running():
            return False
        if gdb_variables_view.is_open() and self.view.id() == gdb_variables_view.get_view().id():
            return True
        return False


class GdbEditRegister(sublime_plugin.TextCommand):
    def run(self, edit):
        row, col = self.view.rowcol(self.view.sel()[0].a)
        reg = gdb_register_view.get_register_at_line(row)
        if not reg is None:
            reg.edit()

    def is_enabled(self):
        if not is_running():
            return False
        print gdb_register_view.is_open()
        print self.view.id() == gdb_register_view.get_view().id()
        if gdb_register_view.is_open() and self.view.id() == gdb_register_view.get_view().id():
            return True
        return False


class GdbEventListener(sublime_plugin.EventListener):
    def on_query_context(self, view, key, operator, operand, match_all):
        if key == "gdb_running":
            return is_running() == operand
        elif key.startswith("gdb_"):
            v = gdb_variables_view
            if key.startswith("gdb_register_view"):
                v = gdb_register_view
            if key.endswith("open"):
                return v.is_open() == operand
            else:
                return (view.id() == v.get_view().id()) == operand
        return None

    def on_activated(self, view):
        if view.file_name() != None:
            update_view_markers(view)

    def on_load(self, view):
        if view.file_name() != None:
            update_view_markers(view)

    def on_close(self, view):
        for v in gdb_views:
            if v.is_open() and view.id() == v.get_view().id():
                v.was_closed()
                break


class GdbOpenSessionView(sublime_plugin.WindowCommand):
    def run(self):
        gdb_session_view.open()

    def is_enabled(self):
        return not gdb_session_view.is_open()

    def is_visible(self):
        return not gdb_session_view.is_open()


class GdbOpenConsoleView(sublime_plugin.WindowCommand):
    def run(self):
        gdb_console_view.open()

    def is_enabled(self):
        return not gdb_console_view.is_open()

    def is_visible(self):
        return not gdb_console_view.is_open()


class GdbOpenVariablesView(sublime_plugin.WindowCommand):
    def run(self):
        gdb_variables_view.open()

    def is_enabled(self):
        return not gdb_variables_view.is_open()

    def is_visible(self):
        return not gdb_variables_view.is_open()


class GdbOpenCallstackView(sublime_plugin.WindowCommand):
    def run(self):
        gdb_callstack_view.open()

    def is_enabled(self):
        return not gdb_callstack_view.is_open()

    def is_visible(self):
        return not gdb_callstack_view.is_open()


class GdbOpenRegisterView(sublime_plugin.WindowCommand):
    def run(self):
        gdb_register_view.open()

    def is_enabled(self):
        return not gdb_register_view.is_open()

    def is_visible(self):
        return not gdb_register_view.is_open()


class GdbOpenDisassemblyView(sublime_plugin.WindowCommand):
    def run(self):
        gdb_disassembly_view.open()

    def is_enabled(self):
        return not gdb_disassembly_view.is_open()

    def is_visible(self):
        return not gdb_disassembly_view.is_open()


class GdbOpenThreadsView(sublime_plugin.WindowCommand):
    def run(self):
        gdb_threads_view.open()

    def is_enabled(self):
        return not gdb_threads_view.is_open()

    def is_visible(self):
        return not gdb_threads_view.is_open()
