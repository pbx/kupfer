from __future__ import annotations

import os
import re
import traceback
import typing as ty
from contextlib import suppress
from pathlib import Path

import gi
from gi.repository import Gdk, Gio, GLib, GObject, Gtk, Pango
from xdg import BaseDirectory as base
from xdg import DesktopEntry as desktop
from xdg import Exceptions as xdg_e

from kupfer import config, icons, launch, plugin_support, version
from kupfer.core import plugins, relevance, settings, sources
from kupfer.obj import KupferObject
from kupfer.support import kupferstring, pretty, scheduler
from kupfer.support import types as kty

from . import accelerators, getkey_dialog, keybindings, kupferhelp
from .credentials_dialog import ask_user_credentials
from .uievents import GUIEnvironmentContext

# index in GtkNotebook
_PLUGIN_LIST_PAGE: ty.Final = 2

# List icon pixel size
_LIST_ICON_SIZE: ty.Final = 18

if ty.TYPE_CHECKING:
    _ = str


def _new_label(  # pylint: disable=keyword-arg-before-vararg
    parent: Gtk.Widget = None,
    /,
    *markup: str,
    selectable: bool = True,
    tooltip: str | None = None,
) -> Gtk.Label:
    text = "".join(markup)
    label = Gtk.Label()
    label.set_alignment(0, 0)  # pylint: disable=no-member
    label.set_padding(0, 4)
    label.set_markup(text)
    label.set_line_wrap(True)  # pylint: disable=no-member
    label.set_selectable(selectable)

    if tooltip:
        label.set_tooltip_text(tooltip)

    if parent:
        parent.pack_start(label, False, True, 0)

    return label


def _format_exc_info(exc_info: kty.ExecInfo) -> str:
    etype, error, _tb = exc_info
    # TRANS: Error message when Plugin needs a Python module to load
    import_error_localized = _("Python module '%s' is needed") % "\\1"
    import_error_pat = r"No module named ([^\s]+)"
    errmsg = str(error)

    if re.match(import_error_pat, errmsg):
        return re.sub(import_error_pat, import_error_localized, errmsg, count=1)

    if etype and issubclass(etype, ImportError):
        return errmsg

    return "".join(traceback.format_exception(*exc_info))


def _kobject_should_show(obj: KupferObject) -> bool:
    with suppress(AttributeError):
        if leaf_repr := obj.get_leaf_repr():  # type: ignore
            if hasattr(leaf_repr, "is_valid") and not leaf_repr.is_valid():
                return False

    return True


def _set_combobox(value: ty.Any, combobox: Gtk.ComboBoxText) -> None:
    """
    Set activate the alternative in the combobox with text value
    """
    value = str(value)
    col = combobox.get_entry_text_column()
    for row in combobox.get_model():
        if row[col] == value:
            combobox.set_active_iter(row.iter)
            return


def _make_combobox_model(combobox: Gtk.ComboBox) -> None:
    # List store with columns (Name, ID)
    combobox_store = Gtk.ListStore(GObject.TYPE_STRING, GObject.TYPE_STRING)
    combobox.set_model(combobox_store)
    combobox_cell = Gtk.CellRendererText()
    combobox.pack_start(combobox_cell, True)
    combobox.add_attribute(combobox_cell, "text", 0)


_KEYBINDING_TARGETS: ty.Final[dict[str, int]] = {
    "keybinding": keybindings.KEYBINDING_TARGET_DEFAULT,
    "magickeybinding": keybindings.KEYBINDING_TARGET_MAGIC,
}

_KUPFER_DESKTOP: ty.Final = "kupfer.desktop"
_AUTOSTART_KEY: ty.Final = "X-GNOME-Autostart-enabled"
_HIDDEN_KEY: ty.Final = "Hidden"


# pylint: disable=too-many-instance-attributes,too-many-public-methods
class PreferencesWindowController(pretty.OutputMixin):
    _instance: PreferencesWindowController | None = None
    _col_plugin_id = 0
    _col_enabled = 1
    _col_icon_name = 2
    _col_text = 3

    @classmethod
    def instance(cls) -> PreferencesWindowController:
        if cls._instance is None:
            cls._instance = PreferencesWindowController()

        return cls._instance

    def __init__(self):
        """Load ui from data file"""
        builder = Gtk.Builder()
        builder.set_translation_domain(version.PACKAGE_NAME)
        self.window: Gtk.Window = None

        if ui_file := config.get_data_file("preferences.ui"):
            builder.add_from_file(ui_file)
        else:
            return

        self.window = builder.get_object("preferenceswindow")
        self.window.set_position(Gtk.WindowPosition.CENTER)
        self.window.connect("delete-event", self._close_window)
        self.plugin_about_parent = builder.get_object("plugin_about_parent")
        self.preferences_notebook = builder.get_object("preferences_notebook")
        self.buttonremovedirectory = builder.get_object("buttonremovedirectory")
        self.entry_plugins_filter = builder.get_object("entry_plugins_filter")
        self.sources_list_ctrl = SourceListController(
            builder.get_object("source_list_parent")
        )

        setctl = settings.get_settings_controller()
        builder.get_object("checkautostart").set_active(
            self._get_should_autostart()
        )

        _set_combobox(
            setctl.get_config_int("Appearance", "icon_large_size"),
            builder.get_object("icons_large_size"),
        )
        _set_combobox(
            setctl.get_config_int("Appearance", "icon_small_size"),
            builder.get_object("icons_small_size"),
        )

        checkstatusicon_gtk = builder.get_object("checkstatusicon_gtk")
        checkstatusicon_gtk.set_label(
            checkstatusicon_gtk.get_label() + " (GtkStatusIcon)"
        )
        checkstatusicon_gtk.set_active(setctl.get_show_status_icon())

        checkstatusicon_ai = builder.get_object("checkstatusicon_ai")
        checkstatusicon_ai.set_label(
            checkstatusicon_ai.get_label() + " (AppIndicator)"
        )
        if _supports_app_indicator():
            checkstatusicon_ai.set_active(setctl.get_show_status_icon_ai())
        else:
            checkstatusicon_ai.set_sensitive(False)

        builder.get_object("checkusecommandkeys").set_active(
            setctl.get_use_command_keys()
        )
        builder.get_object("radio_actionaccelalt").set_active(
            setctl.get_action_accelerator_modifer() != "ctrl"
        )
        builder.get_object("radio_actionaccelctrl").set_active(
            setctl.get_action_accelerator_modifer() == "ctrl"
        )

        # Make alternative comboboxes
        self.terminal_combobox = builder.get_object("terminal_combobox")
        _make_combobox_model(self.terminal_combobox)
        self._update_alternative_combobox("terminal", self.terminal_combobox)

        self.editor_combobox = builder.get_object("editor_combobox")
        _make_combobox_model(self.editor_combobox)
        self._update_alternative_combobox("editor", self.editor_combobox)

        self.icons_combobox = builder.get_object("icons_combobox")
        _make_combobox_model(self.icons_combobox)
        self._update_alternative_combobox("icon_renderer", self.icons_combobox)

        # Plugin List
        self._init_plugin_lists(builder.get_object("plugin_list_parent"))

        # Directory List
        self._init_dir_widgets(builder.get_object("directory_list_parent"))
        self._read_directory_settings()

        # global keybindings list
        self.keybind_table, self.keybind_store = _create_conf_keys_list()
        builder.get_object("keybindings_list_parent").add(self.keybind_table)
        self.keybind_table.connect(
            "row-activated", self.on_keybindings_row_activate
        )
        builder.get_object("button_reset_keys").set_sensitive(
            keybindings.is_available()
        )
        self.keybind_table.set_sensitive(keybindings.is_available())

        # kupfer interface (accelerators) keybindings list
        self._init_keybindings(builder.get_object("gkeybindings_list_parent"))
        self._show_keybindings(setctl)
        self._show_gkeybindings(setctl)

        # Connect to signals at the last point
        builder.connect_signals(self)  # pylint: disable=no-member
        setctl.connect("alternatives-changed", self._on_alternatives_changed)

    def _init_plugin_lists(self, parent: Gtk.Widget) -> None:
        # setup plugin list table
        # cols: ("plugin_id", "enabled", "icon-name", "text")
        self.store = Gtk.ListStore.new((str, bool, str, str))
        self.table = table = Gtk.TreeView.new_with_model(self.store)
        table.set_headers_visible(False)
        table.set_property("enable-search", False)
        table.connect("cursor-changed", self._plugin_table_cursor_changed)
        table.get_selection().set_mode(Gtk.SelectionMode.BROWSE)

        checkcell = Gtk.CellRendererToggle()
        checkcol = Gtk.TreeViewColumn("item", checkcell)
        checkcol.add_attribute(checkcell, "active", self._col_enabled)
        checkcell.connect("toggled", self.on_checkplugin_toggled)

        icon_cell = Gtk.CellRendererPixbuf()
        icon_cell.set_property("height", _LIST_ICON_SIZE)
        icon_cell.set_property("width", _LIST_ICON_SIZE)

        icon_col = Gtk.TreeViewColumn("icon", icon_cell)
        icon_col.add_attribute(icon_cell, "icon-name", self._col_icon_name)

        cell = Gtk.CellRendererText()
        col = Gtk.TreeViewColumn("item", cell)
        col.add_attribute(cell, "text", self._col_text)

        table.append_column(checkcol)
        # hide icon for now
        # self.table.append_column(icon_col)
        table.append_column(col)

        self.plugin_list_timer = scheduler.Timer()
        self.plugin_info = kupferstring.locale_sort(
            plugins.get_plugin_info(), key=lambda rec: rec["localized_name"]
        )

        self._refresh_plugin_list()
        self.output_debug(f"Standard Plugins: {len(self.store)}")
        table.show()

        parent.add(self.table)

    def _init_dir_widgets(self, parent: Gtk.Widget) -> None:
        self.dir_store = Gtk.ListStore.new([str, Gio.Icon, str])
        self.dir_table = dir_table = Gtk.TreeView.new_with_model(self.dir_store)
        dir_table.set_headers_visible(False)
        dir_table.set_property("enable-search", False)
        dir_table.connect("cursor-changed", self._dir_table_cursor_changed)
        dir_table.get_selection().set_mode(Gtk.SelectionMode.BROWSE)

        icon_cell = Gtk.CellRendererPixbuf()
        icon_col = Gtk.TreeViewColumn("icon", icon_cell)
        icon_col.add_attribute(icon_cell, "gicon", 1)

        cell = Gtk.CellRendererText()
        cell.set_property("ellipsize", Pango.EllipsizeMode.END)

        col = Gtk.TreeViewColumn("name", cell)
        col.add_attribute(cell, "text", 2)

        dir_table.append_column(icon_col)
        dir_table.append_column(col)
        dir_table.show()

        parent.add(self.dir_table)

    def _init_keybindings(self, parent: Gtk.Widget) -> None:
        self.gkeybind_table, self.gkeybind_store = _create_conf_keys_list()
        parent.add(self.gkeybind_table)
        self.gkeybind_table.connect(
            "row-activated", self.on_gkeybindings_row_activate
        )

        # Requires GTK 3.22
        with suppress(AttributeError):
            parent.set_propagate_natural_height(True)

    def _show_keybindings(self, setctl: settings.SettingsController) -> None:
        names = (
            # TRANS: Names of global keyboard shortcuts
            (_("Show Main Interface"), "keybinding"),
            (_("Show with Selection"), "magickeybinding"),
        )
        self.keybind_store.clear()
        for name, binding in sorted(names):
            accel = setctl.get_global_keybinding(binding) or ""
            label = Gtk.accelerator_get_label(*Gtk.accelerator_parse(accel))
            self.keybind_store.append((name, label, binding))

    def _show_gkeybindings(self, setctl: settings.SettingsController) -> None:
        names = accelerators.ACCELERATOR_NAMES
        self.gkeybind_store.clear()
        for binding in sorted(names, key=lambda k: str(names[k])):
            accel = setctl.get_accelerator(binding) or ""
            label = Gtk.accelerator_get_label(*Gtk.accelerator_parse(accel))
            self.gkeybind_store.append((names[binding], label, binding))

    def _read_directory_settings(self) -> None:
        setctl = settings.get_settings_controller()
        for directory in setctl.get_directories():
            self._add_directory_model(directory, store=False)

    def _add_directory_model(self, directory: str, store: bool = False) -> None:
        have = [os.path.normpath(row[0]) for row in self.dir_store]
        if directory in have:
            self.output_debug("Ignoring duplicate directory: ", directory)
            return

        have.append(directory)
        directory = os.path.expanduser(directory)
        dispname = launch.get_display_path_for_bytestring(directory)
        gicon = icons.get_gicon_for_file(directory)
        self.dir_store.append((directory, gicon, dispname))

        if store:
            setctl = settings.get_settings_controller()
            setctl.set_directories(have)

    def _remove_directory_model(
        self, rowiter: Gtk.TreeIter, store: bool = True
    ) -> None:
        self.dir_store.remove(rowiter)
        if store:
            have = [os.path.normpath(row[0]) for row in self.dir_store]
            setctl = settings.get_settings_controller()
            setctl.set_directories(have)

    def on_preferenceswindow_key_press_event(
        self, widget: Gtk.Widget, event: Gdk.EventKey
    ) -> bool:
        if event.keyval == Gdk.keyval_from_name("Escape"):
            self._hide()
            return True

        return False

    def on_checkstatusicon_gtk_toggled(self, widget: Gtk.Widget) -> None:
        setctl = settings.get_settings_controller()
        setctl.set_show_status_icon(widget.get_active())

    def on_checkstatusicon_ai_toggled(self, widget: Gtk.Widget) -> None:
        setctl = settings.get_settings_controller()
        setctl.set_show_status_icon_ai(widget.get_active())

    def _get_should_autostart(self) -> bool:
        autostart_dir = base.save_config_path("autostart")
        autostart_file = Path(autostart_dir, _KUPFER_DESKTOP)
        if not autostart_file.exists():
            return False

        try:
            dfile = desktop.DesktopEntry(str(autostart_file))
        except xdg_e.ParsingError as exception:
            pretty.print_error(__name__, exception)
            return False

        return (
            dfile.hasKey(_AUTOSTART_KEY)
            and dfile.get(_AUTOSTART_KEY, type="boolean")
        ) and (
            not dfile.hasKey(_HIDDEN_KEY)
            or not dfile.get(_HIDDEN_KEY, type="boolean")
        )

    def on_checkautostart_toggled(self, widget: Gtk.Widget) -> None:
        autostart_dir = base.save_config_path("autostart")
        autostart_file = Path(autostart_dir, _KUPFER_DESKTOP)
        if not autostart_file.exists():
            desktop_files = list(
                base.load_data_paths("applications", _KUPFER_DESKTOP)
            )
            if not desktop_files:
                self.output_error("Installed kupfer desktop file not found!")
                return

            desktop_file_path = desktop_files[0]
            # Read installed file and modify it
            try:
                dfile = desktop.DesktopEntry(desktop_file_path)
            except xdg_e.ParsingError as exception:
                pretty.print_error(__name__, exception)
                return

            executable = dfile.getExec()
            ## append no-splash
            if "--no-splash" not in executable:
                executable += " --no-splash"

            dfile.set("Exec", executable)
        else:
            try:
                dfile = desktop.DesktopEntry(str(autostart_file))
            except xdg_e.ParsingError as exception:
                pretty.print_error(__name__, exception)
                return

        activestr = str(bool(widget.get_active())).lower()
        not_activestr = str(not bool(widget.get_active())).lower()
        self.output_debug("Setting autostart to", activestr)
        dfile.set(_AUTOSTART_KEY, activestr)
        dfile.set(_HIDDEN_KEY, not_activestr)
        ## remove the format specifiers
        executable = dfile.getExec().replace("%F", "")
        dfile.set("Exec", executable)
        dfile.write(filename=autostart_file)

    def on_helpbutton_clicked(self, widget: Gtk.Widget) -> None:
        kupferhelp.show_help()

    def on_closebutton_clicked(self, widget: Gtk.Widget) -> None:
        self._hide()

    def _refresh_plugin_list(self, us_filter: str | None = None) -> None:
        "List plugins that pass text filter @us_filter or list all if None"
        self.store.clear()
        setctl = settings.get_settings_controller()

        if us_filter:
            self.plugin_list_timer.set_ms(300, self._show_focus_topmost_plugin)
        else:
            self.plugin_list_timer.invalidate()

        for info in self.plugin_info:
            plugin_id = info["name"]
            if setctl.get_plugin_is_hidden(plugin_id):
                continue

            name = info["localized_name"]

            if us_filter:
                name_score = relevance.score(name, us_filter)
                folded_name = kupferstring.tofolded(name)
                fold_name_score = relevance.score(folded_name, us_filter)
                desc_score = relevance.score(info["description"], us_filter)
                if not name_score and not fold_name_score and desc_score < 0.9:
                    continue

            enabled = setctl.get_plugin_enabled(plugin_id)
            self.store.append((plugin_id, enabled, "kupfer-object", str(name)))

    def _show_focus_topmost_plugin(self) -> None:
        try:
            first_row = next(iter(self.store))
        except StopIteration:
            return

        plugin_id = first_row[0]
        self.show_focus_plugin(plugin_id, 0)

    def on_checkplugin_toggled(
        self, cell: Gtk.CellRendererToggle, path: str
    ) -> None:
        plugin_id = self._id_for_table_path(path)
        pathit = self.store.get_iter(path)
        plugin_is_enabled = not self.store.get_value(pathit, self._col_enabled)
        self.store.set_value(pathit, self._col_enabled, plugin_is_enabled)
        setctl = settings.get_settings_controller()
        setctl.set_plugin_enabled(plugin_id, plugin_is_enabled)
        self._plugin_sidebar_update(plugin_id)

    def _id_for_table_path(self, path: str | Gtk.TreePath) -> str:
        pathit = self.store.get_iter(path)
        plugin_id = self.store.get_value(pathit, self._col_plugin_id)
        return plugin_id  # type: ignore

    def _table_path_for_id(self, plugin_id: str) -> Gtk.TreePath:
        """
        Find the tree path of @plugin_id
        """
        for row in self.store:
            if plugin_id == row[self._col_plugin_id]:
                return row.path

        raise ValueError(f"No such plugin {plugin_id}")

    def _plugin_info_for_id(self, plugin_id: str) -> dict[str, ty.Any] | None:
        for info in self.plugin_info:
            if info["name"] == plugin_id:
                return info

        return None

    def _plugin_table_cursor_changed(self, table: Gtk.TreeView) -> None:
        curpath, _curcol = table.get_cursor()
        if not curpath:
            return

        plugin_id = self._id_for_table_path(curpath)
        self._plugin_sidebar_update(plugin_id)

    def _plugin_sidebar_update(self, plugin_id: str) -> None:
        about = Gtk.VBox()
        about.set_property("spacing", 15)
        about.set_property("border-width", 5)
        info = self._plugin_info_for_id(plugin_id)
        if not info:
            return

        _new_label(
            about,
            f'<b><big>{GLib.markup_escape_text(info["localized_name"])}</big></b>',
        )
        ver, description, author = plugins.get_plugin_attributes(
            plugin_id,
            (
                plugins.PluginAttr.VERSION,
                plugins.PluginAttr.DESCRIPTION,
                plugins.PluginAttr.AUTHOR,
            ),
        )
        infobox = Gtk.VBox()
        infobox.set_property("spacing", 3)

        # TRANS: Plugin info fields
        for field, val in (
            (_("Description"), description),
            (_("Author"), author),
        ):
            if val:
                _new_label(infobox, f"<b>{field}</b>")
                _new_label(infobox, GLib.markup_escape_text(val))

        if ver:
            _new_label(
                infobox,
                "<b>",
                _("Version"),
                ":</b> ",
                GLib.markup_escape_text(ver),
            )

        about.pack_start(infobox, False, True, 0)

        # Check for plugin load exception
        if (exc_info := plugins.get_plugin_error(plugin_id)) is not None:
            errstr = _format_exc_info(exc_info)
            _new_label(
                about,
                "<b>",
                _("Plugin could not be read due to an error:"),
                "</b>\n\n",
                GLib.markup_escape_text(errstr),
            )
        elif not plugins.is_plugin_loaded(plugin_id):
            _new_label(about, "(", _("disabled"), ")", selectable=False)

        about.pack_start(
            self._make_plugin_info_widget(plugin_id), False, True, 0
        )
        if psettings_wid := self._make_plugin_settings_widget(plugin_id):
            about.pack_start(psettings_wid, True, True, 0)

        if oldch := self.plugin_about_parent.get_child():
            self.plugin_about_parent.remove(oldch)

        vport = Gtk.Viewport()
        vport.set_shadow_type(Gtk.ShadowType.NONE)  # pylint: disable=no-member
        vport.add(about)  # pylint: disable=no-member
        self.plugin_about_parent.add(vport)
        self.plugin_about_parent.show_all()

    def _make_plugin_info_widget(self, plugin_id: str) -> Gtk.Widget:
        srcs, actions, text_sources = plugins.get_plugin_attributes(
            plugin_id,
            (
                plugins.PluginAttr.SOURCES,
                plugins.PluginAttr.ACTION_DECORATORS,
                plugins.PluginAttr.TEXT_SOURCES,
            ),
        )
        vbox = Gtk.VBox()
        vbox.set_property("spacing", 5)
        setctl = settings.get_settings_controller()
        small_icon_size = setctl.get_config_int("Appearance", "icon_small_size")

        def make_objects_frame(objs: ty.Iterable[str], title: str) -> Gtk.VBox:
            objvbox = Gtk.VBox()
            _new_label(
                objvbox,
                f"<b>{GLib.markup_escape_text(title)}</b>",
                selectable=False,
            )
            objvbox.set_property("spacing", 3)
            for item in objs:
                plugin_type = plugins.get_plugin_attribute(plugin_id, item)
                if not plugin_type:
                    continue

                hbox = Gtk.HBox()
                hbox.set_property("spacing", 3)
                obj = plugin_type()
                image = Gtk.Image()
                image.set_property("gicon", obj.get_icon())
                image.set_property("pixel-size", small_icon_size)
                hbox.pack_start(image, False, True, 0)
                name_label = GLib.markup_escape_text(str(obj))  # name
                if desc := GLib.markup_escape_text(obj.get_description() or ""):
                    name_label = f"{name_label}\n<small>{desc}</small>"

                _new_label(hbox, name_label)

                objvbox.pack_start(hbox, True, True, 0)
                # Display information for application content-sources.
                if not _kobject_should_show(obj):
                    continue

                try:
                    leaf_repr = obj.get_leaf_repr()
                except AttributeError:
                    continue

                if leaf_repr is None:
                    continue

                hbox = Gtk.HBox()
                hbox.set_property("spacing", 3)
                image = Gtk.Image()
                image.set_property("gicon", leaf_repr.get_icon())
                image.set_property("pixel-size", small_icon_size // 2)
                hbox.pack_start(Gtk.Label.new(_("Content of")), False, True, 0)
                hbox.pack_start(image, False, True, 0)
                hbox.pack_start(Gtk.Label.new(str(leaf_repr)), False, True, 0)
                objvbox.pack_start(hbox, True, True, 0)

            return objvbox

        if srcs := list(srcs or ()) + list(text_sources or ()):
            # TRANS: Plugin contents header
            swid = make_objects_frame(srcs, _("Sources"))
            vbox.pack_start(swid, True, True, 0)

        if actions:
            # TRANS: Plugin contents header
            awid = make_objects_frame(actions, _("Actions"))
            vbox.pack_start(awid, True, True, 0)

        vbox.show_all()
        return vbox

    def _get_plugin_change_callback(
        self,
        plugin_id: str,
        key: str,
        value_type: ty.Type[ty.Any],
        get_attr: str,
        no_false_values: bool = False,
    ) -> ty.Callable[[Gtk.Widget], None]:
        """Callback factory for the plugin parameter configuration"""

        def callback(widget: Gtk.Widget) -> None:
            value = getattr(widget, get_attr)()
            if no_false_values and not value:
                return

            setctl = settings.get_settings_controller()
            setctl.set_plugin_config(plugin_id, key, value, value_type)

        return callback

    def _get_plugin_credentials_callback(
        self, plugin_id: str, key: str
    ) -> ty.Callable[[Gtk.Widget], None]:
        # TODO: check, not used / not working probably
        def callback(widget):
            setctl = settings.get_settings_controller()
            val_type = plugin_support.UserNamePassword
            # pylint: disable=no-member
            backend_name = (
                plugin_support.UserNamePassword.get_backend_name()  # type:ignore
            )
            assert backend_name
            # pylint: disable=no-member
            if (
                plugin_support.UserNamePassword.is_backend_encrypted()  # type:ignore
            ):
                information = (
                    _("Using encrypted password storage: %s") % backend_name
                )
            else:
                information = _("Using password storage: %s") % backend_name

            upass = (
                setctl.get_plugin_config(plugin_id, key, val_type)
                or plugin_support.UserNamePassword()
            )
            # pylint: disable=no-member
            user_password = ask_user_credentials(
                upass.username, upass.password, information  # type:ignore
            )
            if user_password:
                # pylint: disable=no-member
                upass.username, upass.password = user_password  # type:ignore
                setctl.set_plugin_config(plugin_id, key, upass, val_type)

        return callback

    # pylint: disable=too-many-locals
    def _make_plugin_settings_widget(self, plugin_id: str) -> Gtk.Widget | None:
        plugin_settings: plugin_support.PluginSettings
        plugin_settings = plugins.get_plugin_attribute(  # type:ignore
            plugin_id, plugins.PluginAttr.SETTINGS
        )
        if not plugin_settings:
            return None

        box = Gtk.Grid()
        box.set_row_spacing(6)
        box.set_column_spacing(12)
        label = _new_label(
            None, f"<b>{_('Configuration')}</b>", selectable=False
        )
        box.attach(label, 0, 0, 2, 1)

        for row, setting in enumerate(plugin_settings, 1):
            label = plugin_settings.get_label(setting)
            typ = plugin_settings.get_value_type(setting)

            if issubclass(typ, plugin_support.UserNamePassword):
                wid = Gtk.Button(label or _("Set username and password"))
                if tooltip := plugin_settings.get_tooltip(setting):
                    wid.set_tooltip_text(tooltip)
                wid.connect(
                    "clicked",
                    self._get_plugin_credentials_callback(plugin_id, setting),
                )
                box.attach(wid, 0, row, 2, 1)
                continue

            if issubclass(typ, bool):
                wid = self._make_plugin_sett_widget_bool(
                    label, plugin_id, setting, plugin_settings
                )
                box.attach(wid, 0, row, 2, 1)
                continue

            tooltip = plugin_settings.get_tooltip(setting)
            wid = _new_label(None, label, selectable=False, tooltip=tooltip)
            box.attach(wid, 0, row, 1, 1)

            if issubclass(typ, str):
                wid = self._make_plugin_sett_widget_str(
                    plugin_id, setting, plugin_settings
                )
                box.attach(wid, 1, row, 1, 1)

            elif issubclass(typ, int):
                wid = self._make_plugin_sett_widget_int(
                    plugin_id, setting, plugin_settings
                )
                box.attach(wid, 1, row, 1, 1)

            elif issubclass(typ, list):
                wid = None
                kind = plugin_settings.get_parameter(setting, "kind")
                if kind == "dirs":
                    wid = self._make_plugin_sett_widget_dirs(
                        plugin_id, setting, plugin_settings
                    )

                if wid:
                    box.attach(wid, 1, row, 1, 1)

        box.show_all()
        return box

    def _make_plugin_sett_widget_str(
        self,
        plugin_id: str,
        setting: str,
        plugin_settings: plugin_support.PluginSettings,
    ) -> Gtk.Widget:
        if alternatives := plugin_settings.get_alternatives(setting):
            wid = self._make_plugin_sett_widget_combo(
                plugin_id, setting, plugin_settings, alternatives
            )

        if plugin_settings.get_parameter(setting, "multiline"):
            wid = self._make_plugin_sett_widget_multiline(
                plugin_id, setting, plugin_settings
            )
        else:
            wid = Gtk.Entry()
            wid.set_text(plugin_settings[setting])
            wid.set_hexpand(True)
            wid.connect(
                "changed",
                self._get_plugin_change_callback(
                    plugin_id,
                    setting,
                    str,
                    "get_text",
                    no_false_values=True,
                ),
            )

        if tooltip := plugin_settings.get_tooltip(setting):
            wid.set_tooltip_text(tooltip)

        return wid

    def _make_plugin_sett_widget_combo(
        self,
        plugin_id: str,
        setting: str,
        plugin_settings: plugin_support.PluginSettings,
        alternatives: ty.Iterable[str],
    ) -> Gtk.Widget:
        wid = Gtk.ComboBoxText.new()
        wid.set_vexpand(False)
        val = plugin_settings[setting]
        active_index = -1
        for idx, text in enumerate(alternatives):
            id_ = text
            if isinstance(text, (tuple, list)):
                id_, text = text

            wid.append(id=id_, text=text)
            if id_ == val:
                active_index = idx

        if active_index < 0:
            wid.prepend_text(val)
            active_index = 0

        wid.set_active(active_index)
        wid.connect(
            "changed",
            self._get_plugin_change_callback(
                plugin_id, setting, str, "get_active_id"
            ),
        )
        return wid

    def _make_plugin_sett_widget_multiline(
        self,
        plugin_id: str,
        setting: str,
        plugin_settings: plugin_support.PluginSettings,
    ) -> Gtk.Widget:
        wid = Gtk.ScrolledWindow()
        wid.set_shadow_type(type=Gtk.ShadowType.IN)
        wid.set_hexpand(True)
        # wid.set_vexpand(True)
        wid.set_size_request(50, 75)
        tview = Gtk.TextView()
        tview.set_border_width(6)
        buf = tview.get_buffer()
        buf.set_text(plugin_settings[setting])

        def callback(widget: Gtk.Widget) -> None:
            start, end = buf.get_bounds()
            value = buf.get_text(start, end, True)
            setctl = settings.get_settings_controller()
            setctl.set_plugin_config(plugin_id, setting, value, str)

        buf.connect("changed", callback)
        wid.add(tview)
        return wid

    def _make_plugin_sett_widget_bool(
        self,
        label: str,
        plugin_id: str,
        setting: str,
        plugin_settings: plugin_support.PluginSettings,
    ) -> Gtk.Widget:
        wid = Gtk.CheckButton.new_with_label(label)
        wid.set_active(plugin_settings[setting])
        if tooltip := plugin_settings.get_tooltip(setting):
            wid.set_tooltip_text(tooltip)

        wid.connect(
            "toggled",
            self._get_plugin_change_callback(
                plugin_id, setting, bool, "get_active"
            ),
        )
        return wid

    def _make_plugin_sett_widget_int(
        self,
        plugin_id: str,
        setting: str,
        plugin_settings: plugin_support.PluginSettings,
    ) -> Gtk.Widget:
        wid = Gtk.SpinButton()
        wid.set_increments(1, 1)
        min_val, max_val = 0, 1000

        if val := plugin_settings.get_parameter(setting, "min"):
            min_val = int(val)

        if val := plugin_settings.get_parameter(setting, "max"):
            max_val = int(val)

        assert max_val > min_val

        wid.set_range(min_val, max_val)
        wid.set_value(plugin_settings[setting])
        wid.set_vexpand(False)
        if tooltip := plugin_settings.get_tooltip(setting):
            wid.set_tooltip_text(tooltip)

        wid.connect(
            "changed",
            self._get_plugin_change_callback(
                plugin_id,
                setting,
                int,
                "get_text",
                no_false_values=True,
            ),
        )
        return wid

    def _make_plugin_sett_widget_dirs(
        self,
        plugin_id: str,
        setting: str,
        plugin_settings: plugin_support.PluginSettings,
    ) -> Gtk.Widget:
        return DirsSelectWidget(plugin_id, setting, plugin_settings)

    def on_buttonadddirectory_clicked(self, widget: Gtk.Widget) -> None:
        # TRANS: File Chooser Title
        chooser_dialog = Gtk.FileChooserDialog(
            title=_("Choose a Directory"),
            action=Gtk.FileChooserAction.SELECT_FOLDER,
            buttons=(
                Gtk.STOCK_CANCEL,
                Gtk.ResponseType.REJECT,
                Gtk.STOCK_OK,
                Gtk.ResponseType.ACCEPT,
            ),
        )
        chooser_dialog.set_select_multiple(True)
        # pylint: disable=no-member
        if chooser_dialog.run() == Gtk.ResponseType.ACCEPT:
            # pylint: disable=no-member
            for selected_dir in chooser_dialog.get_filenames():
                self._add_directory_model(selected_dir, store=True)

        chooser_dialog.hide()

    def on_buttonremovedirectory_clicked(self, widget: Gtk.Widget) -> None:
        curpath, _curcol = self.dir_table.get_cursor()
        if curpath:
            pathit = self.dir_store.get_iter(curpath)
            self._remove_directory_model(pathit, store=True)

    def on_entry_plugins_filter_changed(self, widget: Gtk.Widget) -> None:
        s_filter = widget.get_text()
        us_filter = s_filter.lower()
        self._refresh_plugin_list(us_filter)

    def on_entry_plugins_filter_icon_press(
        self, entry: Gtk.Entry, icon_pos: ty.Any, event: ty.Any
    ) -> None:
        entry.set_text("")

    def on_keybindings_row_activate(
        self,
        treeview: Gtk.TreeView,
        path: Gtk.TreePath,
        view_column: Gtk.TreeViewColumn,
    ) -> None:
        def bind_key_func(target):
            def bind_key(keystr):
                return keybindings.bind_key(keystr, target)

            return bind_key

        pathit = self.keybind_store.get_iter(path)
        keybind_id = self.keybind_store.get_value(pathit, 2)
        setctl = settings.get_settings_controller()
        curr_key = setctl.get_global_keybinding(keybind_id)
        bind_func = bind_key_func(_KEYBINDING_TARGETS[keybind_id])
        keystr = getkey_dialog.ask_for_key(
            bind_func,
            curr_key,
            screen=treeview.get_screen(),
            parent=treeview.get_toplevel(),
        )
        if keystr == "":
            keybindings.bind_key(None, _KEYBINDING_TARGETS[keybind_id])
            setctl.set_global_keybinding(keybind_id, keystr)
            self.keybind_store.set_value(pathit, 1, "")

        elif keystr is not None:
            setctl.set_global_keybinding(keybind_id, keystr)
            label = Gtk.accelerator_get_label(*Gtk.accelerator_parse(keystr))
            self.keybind_store.set_value(pathit, 1, label)

    def _is_good_keystr(self, keystr: str) -> bool:
        # Reject single letters so you can't bind 'A' etc
        if keystr is None:
            return False

        label = Gtk.accelerator_get_label(*Gtk.accelerator_parse(keystr))
        if not label:
            return False

        return not (len(label) == 1 and label.isalnum())

    def on_gkeybindings_row_activate(
        self,
        treeview: Gtk.TreeView,
        path: Gtk.TreePath,
        view_column: Gtk.TreeViewColumn,
    ) -> None:
        pathit = self.gkeybind_store.get_iter(path)
        keybind_id = self.gkeybind_store.get_value(pathit, 2)
        setctl = settings.get_settings_controller()
        curr_key = setctl.get_accelerator(keybind_id)
        keystr = getkey_dialog.ask_for_key(
            self._is_good_keystr,
            previous_key=curr_key,
            screen=treeview.get_screen(),
            parent=treeview.get_toplevel(),
        )

        if keystr is not None:
            setctl.set_accelerator(keybind_id, keystr)
            label = Gtk.accelerator_get_label(*Gtk.accelerator_parse(keystr))
            self.gkeybind_store.set_value(pathit, 1, label)

    def on_button_reset_keys_clicked(self, button: Gtk.Button) -> None:
        if self._ask_user_for_reset_keybinding():
            setctl = settings.get_settings_controller()
            setctl.reset_keybindings()
            self._show_keybindings(setctl)
            # Unbind all before re-binding
            for keybind_id, target in _KEYBINDING_TARGETS.items():
                keybindings.bind_key(None, target)

            for keybind_id, target in _KEYBINDING_TARGETS.items():
                keystr = setctl.get_global_keybinding(keybind_id)
                keybindings.bind_key(keystr, target)

    def on_button_reset_gkeys_clicked(self, button: Gtk.Button) -> None:
        if self._ask_user_for_reset_keybinding():
            setctl = settings.get_settings_controller()
            setctl.reset_accelerators()
            self._show_gkeybindings(setctl)

    def on_checkusecommandkeys_toggled(self, widget: Gtk.Widget) -> None:
        setctl = settings.get_settings_controller()
        setctl.set_use_command_keys(widget.get_active())

    def on_radio_action_accel_alt(self, widget: Gtk.RadioButton) -> None:
        if widget.get_active():
            setctl = settings.get_settings_controller()
            setctl.set_action_accelerator_modifier("alt")

    def on_radio_action_accel_ctrl(self, widget: Gtk.RadioButton) -> None:
        if widget.get_active():
            setctl = settings.get_settings_controller()
            setctl.set_action_accelerator_modifier("ctrl")

    def _dir_table_cursor_changed(self, table: Gtk.TreeView) -> None:
        curpath, _curcol = table.get_cursor()
        if not curpath or not self.dir_store:
            self.buttonremovedirectory.set_sensitive(False)
            return

        self.buttonremovedirectory.set_sensitive(True)

    def on_terminal_combobox_changed(self, widget: Gtk.ComboBox) -> None:
        setctl = settings.get_settings_controller()
        if itr := widget.get_active_iter():
            term_id = widget.get_model().get_value(itr, 1)
            setctl.set_preferred_tool("terminal", term_id)

    def on_editor_combobox_changed(self, widget: Gtk.ComboBox) -> None:
        setctl = settings.get_settings_controller()
        if itr := widget.get_active_iter():
            editor_id = widget.get_model().get_value(itr, 1)
            setctl.set_preferred_tool("editor", editor_id)

    def on_icons_combobox_changed(self, widget: Gtk.ComboBox) -> None:
        setctl = settings.get_settings_controller()
        if itr := widget.get_active_iter():
            term_id = widget.get_model().get_value(itr, 1)
            setctl.set_preferred_tool("icon_renderer", term_id)

    def on_icons_large_size_changed(self, widget: Gtk.ComboBoxText) -> None:
        if widget.get_active_iter():
            val = widget.get_active_text()
            setctl = settings.get_settings_controller()
            setctl.set_large_icon_size(val)

    def on_icons_small_size_changed(self, widget: Gtk.ComboBoxText) -> None:
        if widget.get_active_iter():
            val = widget.get_active_text()
            setctl = settings.get_settings_controller()
            setctl.set_small_icon_size(val)

    def _update_alternative_combobox(
        self, category_key: str, combobox: Gtk.ComboBox
    ) -> None:
        """
        Alternatives changed
        """
        combobox_store = combobox.get_model()
        combobox_store.clear()
        setctl = settings.get_settings_controller()
        term_id = setctl.get_preferred_tool(category_key)
        # fill in the available alternatives
        alternatives = kupferstring.locale_sort(
            setctl.get_valid_alternative_ids(category_key), key=lambda t: t[1]
        )
        term_iter = None
        for id_, name in alternatives:
            _it = combobox_store.append((name, id_))
            if id_ == term_id:
                term_iter = _it

        # Update selection
        term_iter = term_iter or combobox_store.get_iter_first()
        combobox.set_sensitive(len(combobox_store) > 1)
        if term_iter:
            combobox.set_active_iter(term_iter)

    def _on_alternatives_changed(
        self, setctl: settings.SettingsController, category_key: str
    ) -> None:
        if category_key == "terminal":
            self._update_alternative_combobox(
                category_key, self.terminal_combobox
            )

        elif category_key == "editor":
            self._update_alternative_combobox(
                category_key, self.editor_combobox
            )

        elif category_key == "icon_renderer":
            self._update_alternative_combobox(category_key, self.icons_combobox)

    def on_preferences_notebook_switch_page(
        self, notebook: Gtk.Notebook, page: Gtk.Widget, page_num: int
    ) -> None:
        ## focus the search box on the plugin tab
        if page_num == _PLUGIN_LIST_PAGE:
            GLib.idle_add(self.entry_plugins_filter.grab_focus)

    def show(self, timestamp: int) -> None:
        assert self.window
        self.window.present_with_time(timestamp)

    def show_on_screen(self, timestamp: int, screen: Gdk.Screen) -> None:
        assert self.window
        self.window.set_screen(screen)
        self.show(timestamp)
        ## focus the search box on the plugin tab
        if self.preferences_notebook.get_current_page() == _PLUGIN_LIST_PAGE:
            self.entry_plugins_filter.grab_focus()

    def show_focus_plugin(self, plugin_id: str, timestamp: int) -> None:
        """
        Open and show information about plugin @plugin_id
        """
        assert self.window

        try:
            table_path = self._table_path_for_id(plugin_id)
        except ValueError:
            self.entry_plugins_filter.set_text("")
            self._refresh_plugin_list()
            table_path = self._table_path_for_id(plugin_id)

        self.table.set_cursor(table_path)
        self.table.scroll_to_cell(table_path)
        self.preferences_notebook.set_current_page(_PLUGIN_LIST_PAGE)
        self.window.present_with_time(timestamp)

    def _hide(self) -> None:
        assert self.window
        self.window.hide()

    def _close_window(self, *ignored: ty.Any) -> bool:
        self._hide()
        return True

    def _ask_user_for_reset_keybinding(self) -> bool:
        dlg = Gtk.MessageDialog(
            self.window, Gtk.DialogFlags.MODAL, Gtk.MessageType.QUESTION
        )
        dlg.set_markup(_("Reset all shortcuts to default values?"))
        dlg.add_buttons(
            Gtk.STOCK_CANCEL,
            Gtk.ResponseType.CLOSE,
            _("Reset"),
            Gtk.ResponseType.ACCEPT,
        )
        # pylint: disable=no-member
        result: bool = dlg.run() == Gtk.ResponseType.ACCEPT
        dlg.destroy()
        return result


def _create_conf_keys_list() -> tuple[Gtk.TreeView, Gtk.ListStore]:
    columns = (_("Command"), _("Shortcut"), None)
    column_types = (str, str, str)

    keybind_store = Gtk.ListStore.new(column_types)
    keybind_table = Gtk.TreeView.new_with_model(keybind_store)
    for idx, col_header in enumerate(columns):
        renderer = Gtk.CellRendererText()
        column = Gtk.TreeViewColumn(col_header, renderer, text=idx)
        column.set_visible(col_header is not None)
        keybind_table.append_column(column)

    keybind_table.set_property("enable-search", False)
    keybind_table.set_headers_visible(True)
    keybind_table.show()
    return keybind_table, keybind_store


get_preferences_window_controller = PreferencesWindowController.instance


# pylint: disable=too-few-public-methods
class SourceListController:
    def __init__(self, parent_widget):
        # setup plugin list table
        column_types = (GObject.TYPE_PYOBJECT, str, bool, Gio.Icon, str)
        columns = ("source", "plugin_id", "toplevel", "icon", "text")
        self.store = Gtk.ListStore(*column_types)
        self.table = Gtk.TreeView.new_with_model(self.store)
        self.table.set_headers_visible(False)
        self.table.set_property("enable-search", False)
        # self.table.connect("cursor-changed", self.plugin_table_cursor_changed)
        self.table.get_selection().set_mode(Gtk.SelectionMode.NONE)

        checkcell = Gtk.CellRendererToggle()
        checkcol = Gtk.TreeViewColumn("item", checkcell)
        checkcol.add_attribute(checkcell, "active", columns.index("toplevel"))
        checkcell.connect("toggled", self.on_checktoplevel_enabled)

        icon_cell = Gtk.CellRendererPixbuf()
        icon_cell.set_property("height", _LIST_ICON_SIZE)
        icon_cell.set_property("width", _LIST_ICON_SIZE)

        icon_col = Gtk.TreeViewColumn("icon", icon_cell)
        icon_col.add_attribute(icon_cell, "gicon", columns.index("icon"))

        cell = Gtk.CellRendererText()
        col = Gtk.TreeViewColumn("item", cell)
        col.add_attribute(cell, "text", columns.index("text"))

        self.table.append_column(checkcol)
        self.table.append_column(icon_col)
        self.table.append_column(col)

        self._refresh()
        self.table.show()
        parent_widget.add(self.table)

        setctl = settings.get_settings_controller()
        setctl.connect("plugin-enabled-changed", self._refresh)

    def _refresh(self, *ignored: ty.Any) -> None:
        self.store.clear()
        setctl = settings.get_settings_controller()
        srcctl = sources.get_source_controller()
        srcs = sorted(srcctl.get_sources(), key=str)

        for src in srcs:
            plugin_id = srcctl.get_plugin_id_for_object(src)
            if not plugin_id or setctl.get_plugin_is_hidden(plugin_id):
                continue

            if not _kobject_should_show(src):
                continue

            gicon = src.get_icon()
            toplevel = setctl.get_source_is_toplevel(plugin_id, src)
            name = str(src)
            self.store.append((src, plugin_id, toplevel, gicon, name))

    def on_checktoplevel_enabled(
        self, cell: Gtk.CellRendererToggle, path: str
    ) -> None:
        pathit = self.store.get_iter(path)
        is_toplevel = not self.store.get_value(pathit, 2)
        plugin_id = self.store.get_value(pathit, 1)
        src = self.store.get_value(pathit, 0)

        srcctl = sources.get_source_controller()
        srcctl.set_toplevel(src, is_toplevel)

        setctl = settings.get_settings_controller()
        setctl.set_source_is_toplevel(plugin_id, src, is_toplevel)
        self.store.set_value(pathit, 2, is_toplevel)


def _supports_app_indicator() -> bool:
    try:
        gi.require_version("AppIndicator3", "0.1")
    except ValueError:
        return False

    return True


def _get_time(ctxenv: GUIEnvironmentContext | None) -> int:
    if ctxenv:
        return ctxenv.get_timestamp()

    return int(Gtk.get_current_event_time())


def show_preferences(ctxenv: GUIEnvironmentContext) -> None:
    win = get_preferences_window_controller()
    if ctxenv:
        win.show_on_screen(ctxenv.get_timestamp(), ctxenv.get_screen())
    else:
        win.show(_get_time(ctxenv))


def show_plugin_info(
    plugin_id: str, ctxenv: ty.Optional[GUIEnvironmentContext] = None
) -> None:
    prefs = get_preferences_window_controller()
    prefs.show_focus_plugin(plugin_id, _get_time(ctxenv))


class DirsSelectWidget(Gtk.Bin):  # type: ignore
    def __init__(
        self,
        plugin_id: str,
        setting: str,
        plugin_settings: plugin_support.PluginSettings,
    ):
        super().__init__()

        self.plugin_id = plugin_id
        self.setting = setting
        self.plugin_settings = plugin_settings

        self.model = Gtk.ListStore.new([str, Gio.Icon, str])
        self.view = Gtk.TreeView.new_with_model(self.model)
        self.btn_add = Gtk.Button.new_from_stock(Gtk.STOCK_ADD)
        self.btn_del = Gtk.Button.new_from_stock(Gtk.STOCK_REMOVE)

        box = self._create_box()
        self.add(box)

        if dirs := plugin_settings[setting]:
            self._add_dirs(dirs)

    def _create_box(self):
        box = Gtk.VBox()
        box.set_spacing(3)

        view = self.view

        view.set_headers_visible(False)
        view.set_property("enable-search", False)
        view.connect("cursor-changed", self._on_cursor_changed)
        view.get_selection().set_mode(Gtk.SelectionMode.BROWSE)

        icon_cell = Gtk.CellRendererPixbuf()
        icon_col = Gtk.TreeViewColumn("icon", icon_cell)
        icon_col.add_attribute(icon_cell, "gicon", 1)

        cell = Gtk.CellRendererText()
        cell.set_property("ellipsize", Pango.EllipsizeMode.END)

        col = Gtk.TreeViewColumn("name", cell)
        col.add_attribute(cell, "text", 2)

        view.append_column(icon_col)
        view.append_column(col)
        view.show()

        scrollwin = Gtk.ScrolledWindow()
        scrollwin.set_shadow_type(type=Gtk.ShadowType.IN)
        scrollwin.set_hexpand(True)
        scrollwin.set_size_request(50, 100)
        scrollwin.add(view)

        box.pack_start(scrollwin, True, True, 0)

        # buttons
        bbox = Gtk.HButtonBox()
        bbox.set_property("layout-style", Gtk.ButtonBoxStyle.END)

        self.btn_del.connect("clicked", self._on_del_clicked)
        bbox.pack_start(self.btn_del, False, False, 0)

        self.btn_add.connect("clicked", self._on_add_clicked)
        bbox.pack_start(self.btn_add, False, False, 0)

        box.pack_end(bbox, True, True, 0)

        box.set_hexpand(True)
        return box

    def _add_dirs(self, dirs: list[str]) -> None:
        for directory in dirs:
            directory = os.path.expanduser(directory)
            dispname = launch.get_display_path_for_bytestring(directory)
            gicon = icons.get_gicon_for_file(directory)
            self.model.append((directory, gicon, dispname))

    def _on_cursor_changed(self, table: Gtk.TreeView) -> None:
        curpath, _curcol = table.get_cursor()
        self.btn_del.set_sensitive(bool(curpath))

    def _on_del_clicked(self, widget: Gtk.Widget) -> None:
        curpath, _curcol = self.view.get_cursor()
        if curpath:
            rowiter = self.model.get_iter(curpath)
            self.model.remove(rowiter)
            self._save()

    def _on_add_clicked(self, widget: Gtk.Widget) -> None:
        # TRANS: File Chooser Title
        chooser_dialog = Gtk.FileChooserDialog(
            title=_("Choose a Directory"),
            action=Gtk.FileChooserAction.SELECT_FOLDER,
            buttons=(
                Gtk.STOCK_CANCEL,
                Gtk.ResponseType.REJECT,
                Gtk.STOCK_OK,
                Gtk.ResponseType.ACCEPT,
            ),
        )
        chooser_dialog.set_select_multiple(True)

        # pylint: disable=no-member
        if chooser_dialog.run() == Gtk.ResponseType.ACCEPT:
            # pylint: disable=no-member
            have = self.current_dirs()
            new_dirs = [
                directory
                for directory in chooser_dialog.get_filenames()
                if directory not in have
            ]
            self._add_dirs(new_dirs)
            self._save()

        chooser_dialog.hide()

    def current_dirs(self) -> list[str]:
        return [os.path.normpath(row[0]) for row in self.model]

    def _save(self) -> None:
        setctl = settings.get_settings_controller()
        setctl.set_plugin_config(
            self.plugin_id, self.setting, self.current_dirs(), list
        )
