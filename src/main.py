import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk, GLib, Gio, Gdk, Pango

import json
import os
import sys
import threading
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

from PIL import Image, ImageOps

from tagger import Tagger, model_is_cached
from expression_map import classify_expression, score_expressions, ALL_CATEGORIES
from sorter import sort_image

APP_ID = "io.github.hexvi.DesuIndex"
SUPPORTED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"}
THUMBNAIL_SIZE = 180
SETTINGS_FILE = Path(GLib.get_user_config_dir()) / "desuindex" / "settings.json"
SORTED_FILE = Path(GLib.get_user_data_dir()) / "desuindex" / "sorted.json"
# Installed next to this file's folder, in /app/share.
METAINFO_FILE = Path(__file__).resolve().parents[1] / "metainfo" / f"{APP_ID}.metainfo.xml"
MAX_LISTED_FAILURES = 10
SORTED_VERBS = {"copy": "Copied", "move": "Moved", "link": "Linked"}

# The "Show" filter dropdown's entries, in a fixed order.
_FILTER_IDS    = ["all"] + list(ALL_CATEGORIES)
_FILTER_LABELS = ["All"] + [c.capitalize() for c in ALL_CATEGORIES]

CSS = """
flowboxchild {
    border-radius: 12px;
    padding: 6px;
}
flowboxchild:selected {
    background-color: alpha(var(--accent-bg-color), 0.25);
}
"""


def pictures_dir() -> str:
    # Localised XDG dir (~/Pictures, ~/Bilder, …); Ubuntu and Fedora both set it.
    return GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_PICTURES) or str(
        Path.home() / "Pictures"
    )


def count_images(n: int) -> str:
    return f"{n} image{'' if n == 1 else 's'}"


def display_path(path: str) -> str:
    home = str(Path.home())
    if path == home or path.startswith(home + os.sep):
        return "~" + path[len(home):]
    return path


def load_settings() -> dict:
    try:
        return json.loads(SETTINGS_FILE.read_text())
    except (OSError, ValueError):
        return {}


def save_settings(**changes):
    settings = load_settings() | changes
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps(settings, indent=2))


def read_metainfo() -> tuple[str, str]:
    """The newest release's version and the homepage, for the About dialog.

    As for the build, the metainfo is the single source of truth for both.
    """
    try:
        root = ET.parse(METAINFO_FILE).getroot()
    except (OSError, ET.ParseError):
        return "", ""
    release = root.find("releases/release")
    version = release.get("version", "") if release is not None else ""
    return version, root.findtext("url[@type='homepage']", "").strip()


def load_sorted() -> dict[str, str]:
    """Each image copied or linked so far, mapped to where its copy or link went."""
    try:
        return json.loads(SORTED_FILE.read_text())
    except (OSError, ValueError):
        return {}


def record_sorted(sorted_to: dict[str, str]):
    record = load_sorted() | sorted_to
    SORTED_FILE.parent.mkdir(parents=True, exist_ok=True)
    SORTED_FILE.write_text(json.dumps(record))


def already_sorted(image: str, record: dict[str, str], output_folder: str) -> bool:
    """Whether an image's copy or link from an earlier sort is still in output_folder.

    Deleting it, or sorting somewhere else, lets the image be sorted again.
    """
    dest = record.get(image)
    return (
        dest is not None
        and Path(dest).parent.parent == Path(output_folder)
        and os.path.lexists(dest)
    )


def find_images(folder: str, subfolders: bool, output_folder: str) -> list[str]:
    """The supported images in a folder, and optionally the folders inside it.

    Hidden folders are left out, and so is output_folder, so images already
    sorted into it aren't picked up and sorted again.
    """
    images = []
    for parent, dirs, files in os.walk(folder):
        images += (
            os.path.join(parent, f) for f in files
            if Path(f).suffix.lower() in SUPPORTED_EXTENSIONS
        )
        if not subfolders:
            break
        dirs[:] = [
            d for d in dirs
            if not d.startswith(".") and Path(parent, d) != Path(output_folder)
        ]
    return sorted(images)


def make_thumbnail(image_path: str, size: int) -> tuple[int, int, bytes]:
    """Square RGBA thumbnail, safe to call from a worker thread.

    Wide images are cropped from the centre; tall ones from the top, since
    that is where the face usually is (a centre crop beheads full-body art).
    Animated images show their middle frame.
    """
    with Image.open(image_path) as img:
        img.seek(getattr(img, "n_frames", 1) // 2)
        img.draft("RGB", (size, size))  # fast downscaled decode for JPEGs
        img = ImageOps.exif_transpose(img).convert("RGBA")
    img = ImageOps.fit(img, (size, size), Image.LANCZOS, centering=(0.5, 0.0))
    return img.width, img.height, img.tobytes()


class ImageCard(Gtk.Box):
    def __init__(self, image_path: str, name: str, scores, thumbnail):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.image_path = image_path
        # From score_expressions(); None if the image couldn't be tagged.
        self.scores = scores
        # Set when reassigned by hand, which the checked expressions don't undo.
        self.chosen_expression: str | None = None
        self.expression = "other"

        if thumbnail is not None:
            width, height, pixels = thumbnail
            texture = Gdk.MemoryTexture.new(
                width, height, Gdk.MemoryFormat.R8G8B8A8, GLib.Bytes.new(pixels), width * 4
            )
            img_widget = Gtk.Image.new_from_paintable(texture)
        else:
            img_widget = Gtk.Image.new_from_icon_name("image-missing")
        # Gtk.Image draws at icon size (16px) unless told otherwise. The texture
        # may be larger than this on HiDPI screens; it is scaled down crisply.
        img_widget.set_pixel_size(THUMBNAIL_SIZE)

        frame = Gtk.Frame()
        frame.set_halign(Gtk.Align.CENTER)
        frame.set_child(img_widget)
        self.append(frame)

        self._expr_label = Gtk.Label(label=self.expression)
        self._expr_label.add_css_class("heading")
        self.append(self._expr_label)

        # Shortened in the middle, so the file extension stays visible.
        name_label = Gtk.Label(
            label=name, ellipsize=Pango.EllipsizeMode.MIDDLE, max_width_chars=22
        )
        name_label.add_css_class("caption")
        self.append(name_label)

    def classify(self, allowed: set[str]):
        if self.chosen_expression is not None:
            self._show(self.chosen_expression)
        elif self.scores is None:
            self._show("other")
        else:
            self._show(classify_expression(self.scores, allowed))

    def reassign(self, expression: str):
        self.chosen_expression = expression
        self._show(expression)

    def _show(self, expression: str):
        self.expression = expression
        self._expr_label.set_label(expression)


class DesuIndexWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="DesuIndex")
        self.set_default_size(1100, 720)

        self.tagger: Tagger | None = None
        # The cards are the one record of each image's path and expression.
        self.cards: list[ImageCard] = []
        # Set to stop the folder being processed; replaced for each folder.
        self._stop_processing = threading.Event()

        settings = load_settings()
        # The categories images may be sorted into, ticked in the Expressions menu.
        saved = settings.get("expressions")
        if isinstance(saved, list):
            self.allowed_expressions = set(ALL_CATEGORIES) & set(saved)
        else:
            self.allowed_expressions = set(ALL_CATEGORIES)
        # Switched in Preferences; both start off.
        self.include_subfolders = settings.get("include_subfolders") is True
        self.remember_sorted = settings.get("remember_sorted") is True

        self._apply_css()
        self._build_ui()
        self._setup_gestures()

        saved = settings.get("output_folder")
        if saved and Path(saved).is_dir():
            self._set_output_folder(saved)
        else:
            self._set_output_folder(str(Path(pictures_dir()) / "Sorted"))

        self._load_model_async()

    def _apply_css(self):
        provider = Gtk.CssProvider()
        provider.load_from_string(CSS)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(),
            provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )

    def _build_ui(self):
        # Both "Open Folder" buttons (header + empty page) share this action,
        # so enabling/disabling it keeps them in sync.
        self.open_action = Gio.SimpleAction.new("open-folder", None)
        self.open_action.connect("activate", self._on_open_folder)
        self.open_action.set_enabled(False)
        self.add_action(self.open_action)

        for name, callback in (
            ("preferences", self._on_preferences),
            ("about", self._on_about),
        ):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", callback)
            self.add_action(action)

        # ── Header bar ────────────────────────────────────────────────────────
        header = Adw.HeaderBar()
        self.window_title = Adw.WindowTitle(title="DesuIndex")
        header.set_title_widget(self.window_title)

        open_btn = Gtk.Button(
            child=Adw.ButtonContent(icon_name="folder-open-symbolic", label="Open Folder"),
            action_name="win.open-folder",
        )
        header.pack_start(open_btn)

        expressions_btn = Gtk.MenuButton(
            label="Expressions",
            popover=self._build_expressions_popover(),
            tooltip_text="Choose the expressions to sort images into",
        )
        header.pack_start(expressions_btn)

        main_menu = Gio.Menu()
        main_menu.append("_Preferences", "win.preferences")
        main_menu.append("_About DesuIndex", "win.about")
        menu_btn = Gtk.MenuButton(
            icon_name="open-menu-symbolic",
            menu_model=main_menu,
            primary=True,  # opened by F10
            tooltip_text="Main Menu",
        )
        header.pack_end(menu_btn)

        self.sort_btn = Gtk.Button(label="Sort Images")
        self.sort_btn.add_css_class("suggested-action")
        self.sort_btn.connect("clicked", self._on_sort)
        self.sort_btn.set_sensitive(False)
        header.pack_end(self.sort_btn)

        # How Sort Images puts each image in its expression's folder.
        self.sort_mode = Adw.ToggleGroup(valign=Gtk.Align.CENTER)
        for name, tooltip in (
            ("copy", "Copy images into the expression folders"),
            ("move", "Move images into the expression folders"),
            ("link", "Leave images where they are, and put links to them in the "
                     "expression folders"),
        ):
            self.sort_mode.add(Adw.Toggle(name=name, label=name.capitalize(), tooltip=tooltip))
        self.sort_mode.set_active_name("copy")
        header.pack_end(self.sort_mode)

        # ── Filter bar ────────────────────────────────────────────────────────
        filter_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        filter_bar.add_css_class("toolbar")
        filter_bar.append(Gtk.Label(label="Show", margin_start=6))

        self.filter_model = Gtk.StringList.new(_FILTER_LABELS)
        self.filter_combo = Gtk.DropDown.new(self.filter_model, None)
        self.filter_combo.set_selected(0)
        self.filter_combo.connect("notify::selected", self._on_filter_changed)
        filter_bar.append(self.filter_combo)

        # Its counterpart, "Deselect All", is in the selection bar below.
        select_all_btn = Gtk.Button(label="Select All")
        select_all_btn.connect("clicked", self._on_select_all)
        filter_bar.append(select_all_btn)

        filter_spacer = Gtk.Label()
        filter_spacer.set_hexpand(True)
        filter_bar.append(filter_spacer)

        self.count_label = Gtk.Label(label="")
        self.count_label.add_css_class("dim-label")
        self.count_label.set_margin_end(6)
        filter_bar.append(self.count_label)

        # ── Image grid ────────────────────────────────────────────────────────
        self.flow_box = Gtk.FlowBox()
        self.flow_box.set_valign(Gtk.Align.START)
        self.flow_box.set_max_children_per_line(8)
        self.flow_box.set_homogeneous(True)
        self.flow_box.set_row_spacing(6)
        self.flow_box.set_column_spacing(6)
        for side in ("top", "bottom", "start", "end"):
            getattr(self.flow_box, f"set_margin_{side}")(12)
        self.flow_box.set_selection_mode(Gtk.SelectionMode.MULTIPLE)
        self.flow_box.set_filter_func(self._filter_func)
        self.flow_box.connect("selected-children-changed", self._on_selection_changed)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_child(self.flow_box)

        self.status_page = Adw.StatusPage(
            icon_name="folder-pictures-symbolic",
            title="Open a Folder",
            description="Loading the expression model…",
        )
        self.empty_open_btn = Gtk.Button(
            label="Open Folder", action_name="win.open-folder", halign=Gtk.Align.CENTER
        )
        self.empty_open_btn.add_css_class("pill")
        self.empty_open_btn.add_css_class("suggested-action")
        self.status_page.set_child(self.empty_open_btn)

        self.stack = Gtk.Stack()
        self.stack.add_named(self.status_page, "empty")
        self.stack.add_named(scrolled, "grid")
        self.stack.set_visible_child_name("empty")
        self.stack.set_vexpand(True)
        self.stack.add_css_class("view")

        # ── Selection action bar (slides in when items are selected) ──────────
        self.action_bar = Gtk.ActionBar()
        self.action_bar.set_revealed(False)

        self.selection_count_label = Gtk.Label(label="0 selected")
        self.action_bar.pack_start(self.selection_count_label)

        vsep = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
        vsep.set_margin_start(4)
        vsep.set_margin_end(4)
        self.action_bar.pack_start(vsep)
        self.action_bar.pack_start(Gtk.Label(label="Reassign to"))

        self.reassign_model = Gtk.StringList.new(
            [c.capitalize() for c in ALL_CATEGORIES]
        )
        self.reassign_combo = Gtk.DropDown.new(self.reassign_model, None)
        self.reassign_combo.set_selected(0)
        self.action_bar.pack_start(self.reassign_combo)

        apply_btn = Gtk.Button(label="Apply")
        apply_btn.connect("clicked", self._on_reassign)
        self.action_bar.pack_start(apply_btn)

        clear_sel_btn = Gtk.Button(label="Deselect All")
        clear_sel_btn.connect("clicked", lambda _: self.flow_box.unselect_all())
        self.action_bar.pack_end(clear_sel_btn)

        # ── Status bar: status text, progress, destination folder ────────────
        status_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        status_bar.add_css_class("toolbar")

        self.status_label = Gtk.Label(
            label="", xalign=0.0, hexpand=True, ellipsize=Pango.EllipsizeMode.END
        )
        self.status_label.add_css_class("dim-label")
        self.status_label.set_margin_start(6)
        status_bar.append(self.status_label)

        self.progress_bar = Gtk.ProgressBar(valign=Gtk.Align.CENTER, width_request=200)
        self.progress_bar.set_show_text(True)
        self.progress_bar.set_visible(False)
        status_bar.append(self.progress_bar)

        self.stop_btn = Gtk.Button(
            icon_name="process-stop-symbolic",
            valign=Gtk.Align.CENTER,
            tooltip_text="Stop, keeping the images done so far",
        )
        self.stop_btn.add_css_class("flat")
        self.stop_btn.add_css_class("circular")
        self.stop_btn.connect("clicked", self._on_stop_processing)
        self.stop_btn.set_visible(False)
        status_bar.append(self.stop_btn)

        dest_label = Gtk.Label(label="Sort into")
        dest_label.add_css_class("dim-label")
        status_bar.append(dest_label)

        self.dest_content = Adw.ButtonContent(icon_name="folder-symbolic", can_shrink=True)
        self.dest_btn = Gtk.Button(child=self.dest_content)
        self.dest_btn.connect("clicked", self._on_choose_destination)
        status_bar.append(self.dest_btn)

        # ── Assemble ──────────────────────────────────────────────────────────
        toolbar_view = Adw.ToolbarView()
        toolbar_view.add_top_bar(header)
        toolbar_view.add_top_bar(filter_bar)
        toolbar_view.set_content(self.stack)
        toolbar_view.add_bottom_bar(self.action_bar)
        toolbar_view.add_bottom_bar(status_bar)
        self.set_content(toolbar_view)

        # ── Right-click context popover ───────────────────────────────────────
        menu = Gio.Menu()
        for cat in ALL_CATEGORIES:
            menu.append(cat.capitalize(), f"win.reassign-to::{cat}")
        self._context_popover = Gtk.PopoverMenu.new_from_model(menu)
        self._context_popover.set_parent(self.flow_box)
        self._context_popover.set_has_arrow(False)

        reassign_action = Gio.SimpleAction.new("reassign-to", GLib.VariantType("s"))
        reassign_action.connect("activate", self._on_context_sort_to)
        self.add_action(reassign_action)

    def _build_expressions_popover(self) -> Gtk.Popover:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)

        all_none = Gtk.Box(spacing=0, homogeneous=True, margin_bottom=6)
        all_none.add_css_class("linked")
        for label, active in (("All", True), ("None", False)):
            btn = Gtk.Button(label=label)
            btn.connect("clicked", lambda _, a=active: self._check_all_expressions(a))
            all_none.append(btn)
        box.append(all_none)

        # Set while All/None ticks every box, so the grid is re-sorted once.
        self._checking_all = False
        self.expression_checks: dict[str, Gtk.CheckButton] = {}
        for cat in ALL_CATEGORIES:
            if cat == "other":
                box.append(Gtk.Separator(margin_top=4, margin_bottom=4))
            check = Gtk.CheckButton(
                label=cat.capitalize(), active=cat in self.allowed_expressions
            )
            check.connect("toggled", self._on_expressions_toggled)
            self.expression_checks[cat] = check
            box.append(check)

        hint = Gtk.Label(
            label="When unchecked, every image goes to the checked expression "
                  "it's closest to",
            wrap=True, max_width_chars=26, xalign=0.0, margin_start=6,
        )
        hint.add_css_class("caption")
        hint.add_css_class("dim-label")
        box.append(hint)

        return Gtk.Popover(child=box)

    def _setup_gestures(self):
        ctrl = Gtk.GestureClick()
        ctrl.set_button(0)  # handle all mouse buttons
        ctrl.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        ctrl.connect("pressed", self._on_flowbox_click)
        self.flow_box.add_controller(ctrl)

    # ── Main menu ─────────────────────────────────────────────────────────────

    def _on_preferences(self, *_):
        group = Adw.PreferencesGroup(title="Opening Folders")
        for option, title, subtitle in (
            ("include_subfolders", "Include Subfolders",
             "Also scan the folders inside the one you open, except the one "
             "images are sorted into"),
            ("remember_sorted", "Remember Sorted Images",
             "Opening a folder again skips the images already copied or linked "
             "from it"),
        ):
            row = Adw.SwitchRow(title=title, subtitle=subtitle, active=getattr(self, option))
            row.connect("notify::active", self._on_option_toggled, option)
            group.add(row)

        page = Adw.PreferencesPage()
        page.add(group)
        dialog = Adw.PreferencesDialog()
        dialog.add(page)
        dialog.present(self)

    def _on_option_toggled(self, row, _param, option: str):
        setattr(self, option, row.get_active())
        save_settings(**{option: row.get_active()})

    def _on_about(self, *_):
        version, website = read_metainfo()
        Adw.AboutDialog(
            application_icon=APP_ID,
            application_name="DesuIndex",
            developer_name="by hexvi",
            version=version,
            website=website,
            license_type=Gtk.License.GPL_3_0,
            copyright="© 2026 hexvi",
        ).present(self)

    # ── Status ────────────────────────────────────────────────────────────────

    def _status(self, msg: str):
        self.status_label.set_text(msg)

    # ── Model loading ─────────────────────────────────────────────────────────

    def _load_model_async(self):
        if model_is_cached():
            self._status("Loading the expression model…")
        else:
            self._status("Downloading the expression model (first run only, about 1.3 GB)…")

        def worker():
            try:
                GLib.idle_add(self._on_model_ready, Tagger())
            except Exception as e:
                GLib.idle_add(self._on_model_error, str(e))

        threading.Thread(target=worker, daemon=True).start()

    def _on_model_ready(self, tagger):
        self.tagger = tagger
        self.open_action.set_enabled(True)
        self._show_start_page()
        self._status("Model ready — open a folder to begin.")
        return False

    def _show_start_page(self):
        self.window_title.set_subtitle("")
        self.status_page.set_title("Open a Folder")
        self.status_page.set_description(
            "Pick a folder of anime images to sort them by facial expression"
        )
        self.empty_open_btn.set_visible(True)
        self.stack.set_visible_child_name("empty")

    def _on_model_error(self, error):
        self.status_page.set_icon_name("dialog-error-symbolic")
        self.status_page.set_title("Model Failed to Load")
        self.status_page.set_description(error)
        self._status(f"Model failed to load: {error}")
        return False

    # ── Folder selection ──────────────────────────────────────────────────────

    def _pick_folder(self, title: str, accept_label: str, start: Path, on_picked):
        """Ask for a folder, starting in `start`, and pass its path to on_picked."""
        dialog = Gtk.FileDialog(title=title, accept_label=accept_label)
        if not start.is_dir():
            start = start.parent  # e.g. the default ~/Pictures/Sorted may not exist yet
        if start.is_dir():
            dialog.set_initial_folder(Gio.File.new_for_path(str(start)))

        def on_done(dialog, result):
            try:
                folder = dialog.select_folder_finish(result)
            except GLib.Error as e:
                if not e.matches(Gtk.dialog_error_quark(), Gtk.DialogError.DISMISSED):
                    self._status(f"Could not open folder: {e.message}")
                return
            on_picked(folder.get_path())

        dialog.select_folder(self, None, on_done)

    def _on_open_folder(self, *_):
        self._pick_folder(
            "Select Image Folder", "_Open", Path(pictures_dir()), self._process_folder
        )

    # ── Destination folder ────────────────────────────────────────────────────

    def _set_output_folder(self, folder: str):
        self.output_folder = folder
        self.dest_content.set_label(display_path(folder))
        self.dest_btn.set_tooltip_text(f"Sorted images go into {folder}/<expression>/")

    def _on_choose_destination(self, _):
        self._pick_folder(
            "Choose Destination Folder", "_Select", Path(self.output_folder),
            self._on_destination_picked,
        )

    def _on_destination_picked(self, folder: str):
        self._set_output_folder(folder)
        save_settings(output_folder=folder)
        self._status(f"Sorted images will go into {display_path(folder)}")

    # ── Processing ────────────────────────────────────────────────────────────

    def _clear_grid(self):
        # Not get_first_child(): the context popover is also a widget child of
        # flow_box, remove() refuses it, and the loop would never end.
        while (child := self.flow_box.get_child_at_index(0)) is not None:
            self.flow_box.remove(child)

        self.cards = []
        self.action_bar.set_revealed(False)
        self.sort_btn.set_sensitive(False)
        self.count_label.set_text("")

    def _process_folder(self, folder: str):
        images = find_images(folder, self.include_subfolders, self.output_folder)

        if not images:
            where = "that folder or its subfolders" if self.include_subfolders else "that folder"
            self._status(f"No supported images found in {where}.")
            return

        skipped = 0
        if self.remember_sorted:
            record = load_sorted()
            unsorted = [p for p in images if not already_sorted(p, record, self.output_folder)]
            skipped = len(images) - len(unsorted)
            if not unsorted:
                self._status(
                    "Every image in that folder is already sorted into "
                    f"{display_path(self.output_folder)}."
                )
                return
            images = unsorted

        self._clear_grid()
        self.window_title.set_subtitle(display_path(folder))
        self.stack.set_visible_child_name("empty")
        self.status_page.set_title("Processing Images…")
        self.status_page.set_description(None)
        self.empty_open_btn.set_visible(False)
        # The Open Folder buttons are hidden or disabled now, and if focus is
        # left with nowhere to go, GTK moves it onto the first image as it
        # appears. The grid selects whatever has focus, so that image would
        # silently join the next reassign. Park focus on the filter instead.
        self.filter_combo.grab_focus()

        self.progress_bar.set_fraction(0.0)
        self.progress_bar.set_visible(True)
        self.stop_btn.set_sensitive(True)
        self.stop_btn.set_visible(True)
        self.open_action.set_enabled(False)
        skip_note = f", skipping {skipped} already sorted" if skipped else ""
        self._status(f"Processing {count_images(len(images))}{skip_note}…")

        thumb_px = THUMBNAIL_SIZE * self.get_scale_factor()
        stop = self._stop_processing = threading.Event()

        def worker():
            for i, path in enumerate(images):
                if stop.is_set():
                    break
                try:
                    scores = score_expressions(self.tagger.tag(path))
                except Exception:
                    scores = None
                try:
                    thumbnail = make_thumbnail(path, thumb_px)
                except Exception:
                    thumbnail = None
                # Its path inside the folder, telling apart same-named images
                # from different subfolders.
                name = os.path.relpath(path, folder)
                GLib.idle_add(
                    self._on_image_done, path, name, scores, thumbnail, i + 1, len(images)
                )
            # Idle callbacks run in order, so every card is in the grid by now.
            GLib.idle_add(self._on_processing_done, len(images), skipped)

        threading.Thread(target=worker, daemon=True).start()

    def _on_stop_processing(self, _):
        # The worker finishes the image it's on, then stops. Open Folder stays
        # disabled until it has, so two workers never share the tagger.
        self._stop_processing.set()
        self.stop_btn.set_sensitive(False)
        self._status("Stopping after the current image…")

    def _on_image_done(self, path, name, scores, thumbnail, done, total):
        card = ImageCard(path, name, scores, thumbnail)
        card.classify(self.allowed_expressions)
        self.cards.append(card)
        self.flow_box.append(card)
        self.stack.set_visible_child_name("grid")

        self.progress_bar.set_fraction(done / total)
        self.progress_bar.set_text(f"{done} / {total}")
        return False

    def _on_processing_done(self, total, skipped):
        self.progress_bar.set_visible(False)
        self.stop_btn.set_visible(False)
        self.open_action.set_enabled(True)

        done = len(self.cards)
        if done == 0:
            self._show_start_page()
            self._status("Stopped before any images were classified.")
            return False

        self.sort_btn.set_sensitive(True)
        self._update_count_label()
        skip_note = f" ({skipped} skipped as already sorted)" if skipped else ""
        if done < total:
            summary = f"Stopped — {done} of {count_images(total)} classified{skip_note}."
        else:
            summary = f"Done — {count_images(total)} classified{skip_note}."
        self._status(f"{summary} Check them over, then click 'Sort Images'.")
        return False

    # ── Expressions to sort into ──────────────────────────────────────────────

    def _check_all_expressions(self, active: bool):
        self._checking_all = True
        for check in self.expression_checks.values():
            check.set_active(active)
        self._checking_all = False
        self._on_expressions_toggled()

    def _on_expressions_toggled(self, *_):
        if self._checking_all:
            return
        self.allowed_expressions = {
            cat for cat, check in self.expression_checks.items() if check.get_active()
        }
        save_settings(expressions=[c for c in ALL_CATEGORIES if c in self.allowed_expressions])

        if not self.cards:
            return
        # Instant: each card kept its scores, so the model doesn't run again.
        for card in self.cards:
            card.classify(self.allowed_expressions)
        self.flow_box.invalidate_filter()
        self._update_count_label()

    # ── Filter / count ────────────────────────────────────────────────────────

    def _filter_func(self, child):
        selected_id = _FILTER_IDS[self.filter_combo.get_selected()]
        return selected_id in ("all", child.get_child().expression)

    def _on_filter_changed(self, _combo, _param):
        self.flow_box.invalidate_filter()
        self._update_count_label()

    def _update_count_label(self):
        selected_id = _FILTER_IDS[self.filter_combo.get_selected()]
        n = sum(1 for card in self.cards if selected_id in ("all", card.expression))
        self.count_label.set_text(count_images(n))

    # ── Selection ─────────────────────────────────────────────────────────────

    def _on_selection_changed(self, flow_box):
        n = len(flow_box.get_selected_children())
        self.selection_count_label.set_text(f"{n} selected")
        self.action_bar.set_revealed(n > 0)

    def _on_select_all(self, _):
        # Only selects cards that pass the current filter.
        self.flow_box.select_all()

    # ── Click / context menu ──────────────────────────────────────────────────

    def _on_flowbox_click(self, ctrl, n_press, x, y):
        button = ctrl.get_current_button()
        child = self.flow_box.get_child_at_pos(int(x), int(y))

        if button == 1 and n_press == 2:
            if child is not None:
                image = Gio.File.new_for_path(child.get_child().image_path)
                Gtk.FileLauncher.new(image).launch(self, None, None)
            ctrl.set_state(Gtk.EventSequenceState.CLAIMED)
            return

        if button == 1 and n_press == 1:
            if child is None:
                return
            if child in self.flow_box.get_selected_children():
                self.flow_box.unselect_child(child)
            else:
                self.flow_box.select_child(child)
            ctrl.set_state(Gtk.EventSequenceState.CLAIMED)
            return

        if button == 3 and n_press == 1:
            if child is not None and child not in self.flow_box.get_selected_children():
                self.flow_box.unselect_all()
                self.flow_box.select_child(child)
            if self.flow_box.get_selected_children():
                rect = Gdk.Rectangle()
                rect.x, rect.y, rect.width, rect.height = int(x), int(y), 1, 1
                self._context_popover.set_pointing_to(rect)
                self._context_popover.popup()
            ctrl.set_state(Gtk.EventSequenceState.CLAIMED)

    # ── Reassign ──────────────────────────────────────────────────────────────

    def _on_context_sort_to(self, action, parameter):
        self._reassign_selected(parameter.get_string())

    def _on_reassign(self, _):
        self._reassign_selected(ALL_CATEGORIES[self.reassign_combo.get_selected()])

    def _reassign_selected(self, expression: str):
        selected_children = self.flow_box.get_selected_children()
        if not selected_children:
            return

        for child in selected_children:
            child.get_child().reassign(expression)

        self.flow_box.invalidate_filter()
        self.flow_box.unselect_all()
        self._update_count_label()
        self._status(f"Reassigned {count_images(len(selected_children))} to '{expression}'.")

    # ── Sort ──────────────────────────────────────────────────────────────────

    def _on_sort(self, _):
        if not self.cards:
            return

        mode = self.sort_mode.get_active_name()
        verb = mode.capitalize()
        # Exactly what the dialog describes is what gets sorted, even if images
        # are reassigned while the sort runs.
        jobs = [(card.image_path, card.expression) for card in self.cards]

        counts = Counter(expression for _, expression in jobs)
        breakdown = "\n".join(
            f"{expr}: {n}" for expr, n in sorted(counts.items())
        )

        dialog = Adw.AlertDialog(
            heading=f"{verb} {count_images(len(jobs))}?",
            body=f"Into {display_path(self.output_folder)}\n\n{breakdown}",
        )
        dialog.add_response("cancel", "_Cancel")
        dialog.add_response("sort", f"_{verb}")
        dialog.set_response_appearance(
            "sort",
            Adw.ResponseAppearance.DESTRUCTIVE if mode == "move"
            else Adw.ResponseAppearance.SUGGESTED,
        )
        dialog.set_default_response("sort")
        dialog.set_close_response("cancel")
        # Pin the destination now, so changing it mid-sort can't split the batch.
        dialog.connect(
            "response", self._on_sort_dialog_response, jobs, mode, self.output_folder
        )
        dialog.present(self)

    def _on_sort_dialog_response(self, dialog, response, jobs, mode, output_folder):
        if response != "sort":
            return

        # Opening another folder mid-sort would be wiped when a move finishes.
        self.sort_btn.set_sensitive(False)
        self.open_action.set_enabled(False)
        self._status("Sorting…")

        def worker():
            failures = []
            sorted_to = {}
            for path, expression in jobs:
                try:
                    sorted_to[path] = sort_image(path, expression, output_folder, mode)
                except Exception as e:
                    failures.append(f"{Path(path).name}: {e}")
            GLib.idle_add(
                self._on_sort_done, len(jobs), failures, mode, output_folder, sorted_to
            )

        threading.Thread(target=worker, daemon=True).start()

    def _on_sort_done(self, total, failures, mode, output_folder, sorted_to):
        self._status(
            f"{SORTED_VERBS[mode]} {count_images(total - len(failures))} to "
            f"{display_path(output_folder)}"
        )
        self.open_action.set_enabled(True)

        if mode == "move":
            # The grid still points at the images' old locations, so sorting
            # again or opening them would fail.
            self._clear_grid()
            self._show_start_page()
        else:
            # Remembered so rescans skip them. Moved images have left the
            # folder, so they need no record.
            if self.remember_sorted:
                record_sorted(sorted_to)
            self.sort_btn.set_sensitive(True)

        if failures:
            self._show_sort_failures(failures)
        return False

    def _show_sort_failures(self, failures: list[str]):
        # Reported in a dialog: the Flatpak has no terminal to print to.
        body = "\n".join(failures[:MAX_LISTED_FAILURES])
        if len(failures) > MAX_LISTED_FAILURES:
            body += f"\n…and {len(failures) - MAX_LISTED_FAILURES} more"
        dialog = Adw.AlertDialog(
            heading=f"{count_images(len(failures))} couldn't be sorted", body=body
        )
        dialog.add_response("close", "_Close")
        dialog.present(self)


class DesuIndexApp(Adw.Application):
    def __init__(self):
        super().__init__(
            application_id=APP_ID,
            flags=Gio.ApplicationFlags.DEFAULT_FLAGS,
        )

    def do_activate(self):
        self.set_accels_for_action("win.preferences", ["<Control>comma"])
        win = DesuIndexWindow(self)
        win.present()


if __name__ == "__main__":
    app = DesuIndexApp()
    app.run(sys.argv)
