"""A fake Tkinter, so the GUI can be tested without a display.

Tk needs a real window server, which CI boxes and headless servers do not
have. Rather than skip GUI testing entirely, we install this stand-in into
``sys.modules`` before importing ``share_ocr.gui``. Every widget records the
keyword arguments, geometry-manager calls and event bindings it receives, so
tests can assert on real behaviour: is the model picker hidden, did closing
the window cancel the timer, does one file produce exactly one table row.

It is deliberately permissive - unknown methods return None instead of
blowing up - because the goal is to exercise *our* logic, not to reimplement
Tk.
"""
from __future__ import annotations

import sys
import types


class TclError(Exception):
    pass


class Variable:
    def __init__(self, master=None, value=None, name=None):
        self._value = value if value is not None else ""

    def get(self):
        return self._value

    def set(self, value):
        self._value = value


class StringVar(Variable):
    def __init__(self, master=None, value="", name=None):
        super().__init__(master, value if value is not None else "", name)


class IntVar(Variable):
    def __init__(self, master=None, value=0, name=None):
        super().__init__(master, value if value is not None else 0, name)


class BooleanVar(Variable):
    def __init__(self, master=None, value=False, name=None):
        super().__init__(master, bool(value), name)


class Widget:
    """Base stand-in for every Tk / ttk widget."""

    def __init__(self, master=None, **kw):
        self.master = master
        self.kw = dict(kw)
        self.children = []
        self.bindings = {}
        self.packed = False
        self.pack_kw = {}
        self.destroyed = False
        if isinstance(master, Widget):
            master.children.append(self)

    # -- config -------------------------------------------------------
    def configure(self, cnf=None, **kw):
        if isinstance(cnf, dict):
            self.kw.update(cnf)
        self.kw.update(kw)
        return None

    config = configure

    def cget(self, key):
        return self.kw.get(key, "")

    def __getitem__(self, key):
        return self.cget(key)

    def __setitem__(self, key, value):
        self.kw[key] = value

    def keys(self):
        return list(self.kw)

    # -- geometry -----------------------------------------------------
    def pack(self, **kw):
        self.packed = True
        self.pack_kw = kw
        return None

    def pack_forget(self):
        self.packed = False

    def pack_propagate(self, flag=None):
        return None

    def grid(self, **kw):
        self.packed = True
        self.pack_kw = kw

    def grid_forget(self):
        self.packed = False

    def place(self, **kw):
        self.packed = True

    def destroy(self):
        self.destroyed = True
        if isinstance(self.master, Widget) and self in self.master.children:
            self.master.children.remove(self)

    def winfo_children(self):
        return list(self.children)

    def winfo_reqwidth(self):
        return 900

    def winfo_reqheight(self):
        return 600

    def winfo_width(self):
        return 1000

    def winfo_height(self):
        return 700

    def winfo_x(self):
        return 0

    def winfo_y(self):
        return 0

    def winfo_screenwidth(self):
        return 1366           # the laptop size that exposed the sizing bug

    def winfo_screenheight(self):
        return 768

    def winfo_exists(self):
        return not self.destroyed

    def winfo_toplevel(self):
        return self

    # -- events -------------------------------------------------------
    def bind(self, sequence=None, func=None, add=None):
        self.bindings[sequence] = func
        return "binding"

    def unbind(self, sequence, funcid=None):
        self.bindings.pop(sequence, None)

    def fire(self, sequence, event=None):
        """Test helper: pretend the user triggered an event."""
        fn = self.bindings.get(sequence)
        if fn:
            return fn(event)
        return None

    def focus_set(self):
        return None

    def update_idletasks(self):
        return None

    def update(self):
        return None

    def __getattr__(self, name):
        # Anything we did not model is a no-op, e.g. dnd_bind, iconphoto.
        def _noop(*a, **k):
            return None
        return _noop


class Misc(Widget):
    pass


class Canvas(Widget):
    def __init__(self, master=None, **kw):
        super().__init__(master, **kw)
        self.items = []

    def delete(self, *a):
        self.items = []

    def create_polygon(self, *a, **kw):
        self.items.append(("polygon", a, kw))
        return len(self.items)

    def create_text(self, *a, **kw):
        self.items.append(("text", a, kw))
        return len(self.items)

    def create_rectangle(self, *a, **kw):
        self.items.append(("rect", a, kw))
        return len(self.items)

    def create_line(self, *a, **kw):
        self.items.append(("line", a, kw))
        return len(self.items)


class Frame(Widget):
    pass


class Label(Widget):
    pass


class Entry(Widget):
    pass


class Text(Widget):
    pass


class Toplevel(Widget):
    def __init__(self, master=None, **kw):
        super().__init__(master, **kw)
        self.title_text = ""

    def title(self, text=None):
        if text is not None:
            self.title_text = text
        return self.title_text

    def protocol(self, name=None, func=None):
        self.bindings["protocol:" + str(name)] = func

    def transient(self, other=None):
        return None

    def grab_set(self):
        return None

    def wait_window(self, *a):
        return None

    def geometry(self, spec=None):
        if spec is not None:
            self.kw["geometry"] = spec
        return self.kw.get("geometry", "400x300+0+0")


class Tk(Toplevel):
    """Root window. Records after() timers so tests can prove they stop."""

    def __init__(self, *a, **kw):
        super().__init__(None, **kw)
        self.after_calls = []          # every scheduled callback
        self.cancelled = []            # every cancelled id
        self._after_seq = 0
        self._state = "normal"
        self.mainloop_ran = False
        self.min_size = None

    def title(self, text=None):
        return super().title(text)

    def minsize(self, w=None, h=None):
        if w is not None:
            self.min_size = (w, h)
        return self.min_size

    def maxsize(self, w=None, h=None):
        return None

    def resizable(self, w=None, h=None):
        return None

    def state(self, value=None):
        if value is not None:
            self._state = value
        return self._state

    def attributes(self, *a, **k):
        return None

    def after(self, ms, func=None, *args):
        self._after_seq += 1
        tid = "after#%d" % self._after_seq
        self.after_calls.append((tid, ms, func))
        return tid

    def after_cancel(self, tid):
        self.cancelled.append(tid)

    def after_idle(self, func=None, *a):
        return self.after(0, func)

    def mainloop(self, n=0):
        self.mainloop_ran = True

    def quit(self):
        return None

    def run_pending(self, limit=50):
        """Test helper: run scheduled callbacks that were not cancelled."""
        pending = [c for c in self.after_calls if c[0] not in self.cancelled]
        self.after_calls = []
        for _tid, _ms, func in pending[:limit]:
            if func:
                func()


class PhotoImage(Widget):
    pass


# --------------------------------------------------------------- ttk ----
class Style(Widget):
    def __init__(self, master=None):
        super().__init__(master)
        self.configured = {}
        self.mapped = {}

    def theme_use(self, name=None):
        return "clam"

    def theme_names(self):
        return ["clam", "alt", "default"]

    def configure(self, style=None, **kw):            # type: ignore[override]
        if isinstance(style, str):
            self.configured.setdefault(style, {}).update(kw)
        return None

    def map(self, style=None, **kw):
        if isinstance(style, str):
            self.mapped.setdefault(style, {}).update(kw)
        return None

    def layout(self, style=None, layoutspec=None):
        return []

    def element_create(self, *a, **k):
        return None

    def lookup(self, *a, **k):
        return ""


class Combobox(Widget):
    def set(self, value):
        var = self.kw.get("textvariable")
        if var is not None:
            var.set(value)

    def get(self):
        var = self.kw.get("textvariable")
        return var.get() if var is not None else ""

    def current(self, index=None):
        return 0


class Treeview(Widget):
    def __init__(self, master=None, **kw):
        super().__init__(master, **kw)
        self.rows = {}                 # iid -> values
        self.order = []                # newest-first / insertion order
        self.headings = {}
        self.columns_cfg = {}
        self.tags = {}
        self._seq = 0

    def heading(self, key, **kw):
        self.headings[key] = kw

    def column(self, key, **kw):
        self.columns_cfg[key] = kw

    def tag_configure(self, tag, **kw):
        self.tags[tag] = kw

    def insert(self, parent, index, iid=None, values=(), tags=(), **kw):
        self._seq += 1
        iid = iid or "I%d" % self._seq
        self.rows[iid] = {"values": tuple(values), "tags": tuple(tags)}
        if index == 0 or index == "0":
            self.order.insert(0, iid)
        else:
            self.order.append(iid)
        return iid

    def delete(self, *iids):
        for iid in iids:
            self.rows.pop(iid, None)
            if iid in self.order:
                self.order.remove(iid)

    def get_children(self, item=""):
        return tuple(self.order)

    def item(self, iid, option=None, **kw):
        row = self.rows.get(iid, {})
        if option == "values":
            return row.get("values", ())
        return row

    def selection(self):
        return tuple(self.order[:1])

    def yview(self, *a):
        return None

    def xview(self, *a):
        return None


class Progressbar(Widget):
    pass


class Scrollbar(Widget):
    def set(self, *a):
        return None


class Notebook(Widget):
    pass


# ------------------------------------------------------- dialog spies ---
class _DialogSpy:
    """Records dialog calls; tests assert on them."""

    def __init__(self):
        self.calls = []
        self.askyesno_result = True
        self.save_path = ""
        self.open_paths = ()
        self.directory = ""

    def reset(self):
        self.calls = []

    # messagebox
    def showinfo(self, title=None, message=None, **k):
        self.calls.append(("showinfo", title, message))

    def showwarning(self, title=None, message=None, **k):
        self.calls.append(("showwarning", title, message))

    def showerror(self, title=None, message=None, **k):
        self.calls.append(("showerror", title, message))

    def askyesno(self, title=None, message=None, **k):
        self.calls.append(("askyesno", title, message))
        return self.askyesno_result

    def askokcancel(self, title=None, message=None, **k):
        self.calls.append(("askokcancel", title, message))
        return self.askyesno_result

    # filedialog
    def askopenfilenames(self, **k):
        self.calls.append(("askopenfilenames", None, None))
        return self.open_paths

    def askdirectory(self, **k):
        self.calls.append(("askdirectory", None, None))
        return self.directory

    def asksaveasfilename(self, **k):
        self.calls.append(("asksaveasfilename", None, None))
        return self.save_path


DIALOGS = _DialogSpy()


def install():
    """Put the fake modules into sys.modules. Call before importing the GUI."""
    tk = types.ModuleType("tkinter")
    for name in ("Tk", "Toplevel", "Frame", "Label", "Entry", "Text", "Canvas",
                 "Widget", "Misc", "StringVar", "IntVar", "BooleanVar",
                 "Variable", "PhotoImage", "TclError"):
        setattr(tk, name, globals()[name])
    for const in ("LEFT", "RIGHT", "TOP", "BOTTOM", "BOTH", "X", "Y", "W",
                  "E", "NS", "EW", "NSEW", "END", "NORMAL", "DISABLED"):
        setattr(tk, const, const.lower())

    ttk = types.ModuleType("tkinter.ttk")
    ttk.Frame = Frame
    ttk.Label = Label
    ttk.Entry = Entry
    ttk.Button = Widget
    ttk.Checkbutton = Widget
    ttk.Radiobutton = Widget
    ttk.Separator = Widget
    ttk.Spinbox = Widget
    ttk.Combobox = Combobox
    ttk.Treeview = Treeview
    ttk.Progressbar = Progressbar
    ttk.Scrollbar = Scrollbar
    ttk.Notebook = Notebook
    ttk.Style = Style
    tk.ttk = ttk

    fontmod = types.ModuleType("tkinter.font")

    class _Font:
        def __init__(self, *a, **k):
            self._size = 10

        def measure(self, text):
            return max(8, len(str(text)) * 7)

        def metrics(self, *a):
            return 16

        def actual(self, *a):
            return {"family": "Segoe UI", "size": 10}

        def configure(self, **k):
            return None

    fontmod.Font = _Font
    fontmod.families = lambda *a, **k: ("Segoe UI", "Arial", "Consolas")
    fontmod.nametofont = lambda *a, **k: _Font()
    tk.font = fontmod

    mb = types.ModuleType("tkinter.messagebox")
    for fn in ("showinfo", "showwarning", "showerror", "askyesno", "askokcancel"):
        setattr(mb, fn, getattr(DIALOGS, fn))

    fd = types.ModuleType("tkinter.filedialog")
    for fn in ("askopenfilenames", "askdirectory", "asksaveasfilename"):
        setattr(fd, fn, getattr(DIALOGS, fn))

    tk.messagebox = mb
    tk.filedialog = fd

    sys.modules["tkinter"] = tk
    sys.modules["tkinter.ttk"] = ttk
    sys.modules["tkinter.font"] = fontmod
    sys.modules["tkinter.messagebox"] = mb
    sys.modules["tkinter.filedialog"] = fd
    return tk
