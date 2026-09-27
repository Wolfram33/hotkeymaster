# Copyright 2026 Wolfram Consult GmbH & Co. KG
# SPDX-License-Identifier: Apache-2.0

"""Hotkey-Master – systemweite Tastenkürzel für Windows.

Ein Hotkey kann Text tippen, ein Programm/eine Datei öffnen oder eine andere
Tastenkombination senden.

Autor: Rob de Roy
"""

import json
import logging
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PyQt6.QtCore import QDir, QEvent, QLockFile, QObject, Qt, pyqtSignal
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import (QApplication, QDialog, QFileDialog, QGridLayout,
                             QGroupBox, QHBoxLayout, QLabel, QLineEdit,
                             QListWidget, QListWidgetItem, QMainWindow,
                             QMessageBox, QComboBox, QPushButton, QTabWidget,
                             QTextBrowser, QVBoxLayout, QWidget)
# Pyright/Pylance cannot resolve the external pynput package in some IDE setups.
# pyright: reportMissingModuleSource=false
from pynput import keyboard as pynput_keyboard
import win32api
import win32con

APP_NAME = 'Hotkey-Master'
APP_VERSION = '1.6.0'
APP_AUTHOR = 'Rob de Roy'

log = logging.getLogger('hotkey-master')


# --- Tastennamen <-> virtuelle Tastencodes ----------------------------------
# Hotkeys werden über virtuelle Tastencodes erkannt statt über das erzeugte
# Zeichen: Strg/AltGr verändern das Zeichen (Strg+E -> '\x05', AltGr+E -> '€'),
# der Tastencode bleibt gleich.

MODIFIERS = ('ctrl', 'alt', 'shift', 'win')  # auch die Anzeige-Reihenfolge

NAME_TO_VK = {
    'ctrl': win32con.VK_CONTROL, 'alt': win32con.VK_MENU,
    'shift': win32con.VK_SHIFT, 'win': win32con.VK_LWIN,
    'space': win32con.VK_SPACE, 'enter': win32con.VK_RETURN,
    'tab': win32con.VK_TAB, 'esc': win32con.VK_ESCAPE,
    'backspace': win32con.VK_BACK, 'delete': win32con.VK_DELETE,
    'insert': win32con.VK_INSERT, 'home': win32con.VK_HOME,
    'end': win32con.VK_END, 'pageup': win32con.VK_PRIOR,
    'pagedown': win32con.VK_NEXT, 'left': win32con.VK_LEFT,
    'up': win32con.VK_UP, 'right': win32con.VK_RIGHT,
    'down': win32con.VK_DOWN, 'printscreen': win32con.VK_SNAPSHOT,
    'pause': win32con.VK_PAUSE,
}
NAME_TO_VK.update({f'f{i}': win32con.VK_F1 + i - 1 for i in range(1, 25)})
NAME_TO_VK.update({c: ord(c.upper()) for c in '0123456789abcdefghijklmnopqrstuvwxyz'})

VK_TO_NAME = {vk: name for name, vk in NAME_TO_VK.items()}
VK_TO_NAME.update({
    win32con.VK_LCONTROL: 'ctrl', win32con.VK_RCONTROL: 'ctrl',
    win32con.VK_LMENU: 'alt', win32con.VK_RMENU: 'alt',
    win32con.VK_LSHIFT: 'shift', win32con.VK_RSHIFT: 'shift',
    win32con.VK_RWIN: 'win',
})

# Tasten, die beim Senden das Extended-Flag brauchen (sonst z. B. Ziffernblock-Pfeile)
EXTENDED_VKS = {
    win32con.VK_INSERT, win32con.VK_DELETE, win32con.VK_HOME, win32con.VK_END,
    win32con.VK_PRIOR, win32con.VK_NEXT, win32con.VK_LEFT, win32con.VK_UP,
    win32con.VK_RIGHT, win32con.VK_DOWN, win32con.VK_LWIN, win32con.VK_RWIN,
    win32con.VK_SNAPSHOT,
}

MODIFIER_VKS = (win32con.VK_CONTROL, win32con.VK_MENU, win32con.VK_SHIFT,
                win32con.VK_LWIN, win32con.VK_RWIN)

# Tasten, die auch ohne Strg/Alt/Win als Auslöser taugen, weil sie beim
# normalen Schreiben nicht vorkommen
STANDALONE_KEYS = {f'f{i}' for i in range(1, 25)} | {'pause'}


def is_key_down(vk):
    return bool(win32api.GetAsyncKeyState(vk) & 0x8000)


def normalize_combo(combo):
    """'E + Ctrl+alt' -> 'ctrl+alt+e'. Wirft ValueError mit Klartext-Meldung."""
    if not isinstance(combo, str):
        raise ValueError('Tastenkombination fehlt.')
    keys = {part.strip().lower() for part in combo.split('+') if part.strip()}
    if not keys:
        raise ValueError('Keine Taste angegeben – z. B. ctrl+alt+n.')
    unknown = sorted(keys - NAME_TO_VK.keys())
    if unknown:
        raise ValueError(f'Unbekannte Taste: {", ".join(unknown)}. Erlaubt sind '
                         'ctrl, alt, shift, win, a–z, 0–9, f1–f24, space, enter, '
                         'tab, esc, backspace, delete, insert, home, end, pageup, '
                         'pagedown, left, up, right, down, printscreen, pause.')
    mods = [m for m in MODIFIERS if m in keys]
    return '+'.join(mods + sorted(keys - set(MODIFIERS)))


def validate_trigger(combo):
    """Prüft, ob eine Kombination als Auslöser taugt; liefert die Normalform."""
    combo = normalize_combo(combo)
    keys = set(combo.split('+'))
    main_keys = keys - set(MODIFIERS)
    if not main_keys:
        raise ValueError('Nur Modifier-Tasten gewählt – bitte zusätzlich eine '
                         'normale Taste wählen, z. B. ctrl+alt+n.')
    if not keys & {'ctrl', 'alt', 'win'} and not main_keys <= STANDALONE_KEYS:
        raise ValueError('Ohne Strg, Alt oder Win würde dieser Hotkey beim '
                         'normalen Schreiben auslösen – bitte einen dieser Modifier '
                         'hinzufügen. (F-Tasten und Pause gehen auch allein.)')
    return combo


# Anordnung der Bildschirm-Tastatur und deutsche Anzeigenamen der Tasten
KEYBOARD_ROWS = [
    [('Strg', 'ctrl'), ('Alt', 'alt'), ('Umschalt', 'shift'), ('Win', 'win')],
    [(f'F{i}', f'f{i}') for i in range(1, 13)],
    [(c, c) for c in '1234567890'],
    [(c.upper(), c) for c in 'qwertzuiop'],
    [(c.upper(), c) for c in 'asdfghjkl'],
    [(c.upper(), c) for c in 'yxcvbnm'],
    [('Leertaste', 'space'), ('Enter', 'enter'), ('Tab', 'tab'), ('Esc', 'esc'),
     ('Rücktaste', 'backspace'), ('Entf', 'delete'), ('Einfg', 'insert'), ('Pause', 'pause')],
    [('Pos1', 'home'), ('Ende', 'end'), ('Bild ↑', 'pageup'), ('Bild ↓', 'pagedown'),
     ('←', 'left'), ('↑', 'up'), ('→', 'right'), ('↓', 'down')],
]
KEY_LABELS = {key: label for row in KEYBOARD_ROWS for label, key in row}
KEY_LABELS['printscreen'] = 'Druck'


def format_combo(combo):
    """'ctrl+alt+n' -> 'Strg + Alt + N' für die Anzeige."""
    return ' + '.join(KEY_LABELS.get(k, k.upper()) for k in combo.split('+'))


def resource_path(name):
    # Nuitka --onefile entpackt Datendateien neben das Modul
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), name)


# --- Aktionen ----------------------------------------------------------------

ACTIONS = {
    'type_text': {
        'name': 'Text eingeben',
        'field': 'Te&xt:',
        'placeholder': 'Text, der getippt wird – z. B. name@firma.de',
        'hint': 'Wird Zeichen für Zeichen getippt. Der Text erscheint aus '
                'Datenschutzgründen nicht in der Liste.',
        'missing': 'Bitte den Text eingeben, der getippt werden soll.',
    },
    'open_program': {
        'name': 'Programm/Datei öffnen',
        'field': '&Programm/Datei:',
        'placeholder': r'Pfad oder Programmname – z. B. C:\Windows\System32\calc.exe',
        'hint': 'Auch Dateien, Ordner und Programmnamen wie „notepad.exe“ sind möglich.',
        'missing': 'Bitte das Programm oder die Datei angeben – „Durchsuchen …“ hilft beim Finden.',
    },
    'custom_keys': {
        'name': 'Tastenkombination senden',
        'field': 'Zu &sendende Tasten:',
        'placeholder': 'z. B. ctrl+s oder ctrl+shift+esc',
        'hint': 'Englische Tastennamen mit + verbinden – Übersicht im Reiter „Info“.',
        'missing': 'Bitte die zu sendenden Tasten eingeben, z. B. ctrl+s.',
    },
}


def validate_action(action_type, param):
    """Prüft einen Aktions-Parameter; liefert ihn (ggf. normalisiert) zurück."""
    if action_type not in ACTIONS:
        raise ValueError(f'Unbekannte Aktion „{action_type}“.')
    if not isinstance(param, str) or not param.strip():
        raise ValueError(ACTIONS[action_type]['missing'])
    if action_type == 'custom_keys':
        return normalize_combo(param)
    if action_type == 'open_program':
        return param.strip()
    return param  # Text unverändert lassen, auch führende/folgende Leerzeichen


class ActionRunner(QObject):
    """Führt Hotkey-Aktionen nacheinander in einem Hintergrund-Thread aus."""
    finished = pyqtSignal(str)
    failed = pyqtSignal(str)

    TYPE_DELAY = 0.01           # Pause zwischen Zeichen; manche Apps verschlucken sonst Eingaben
    MODIFIER_RELEASE_TIMEOUT = 3.0

    def __init__(self):
        super().__init__()
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='hotkey-action')
        self._keyboard = pynput_keyboard.Controller()

    def submit(self, combo, action):
        self._pool.submit(self._run, combo, action)

    def shutdown(self):
        self._pool.shutdown(wait=False, cancel_futures=True)

    def _run(self, combo, action):
        action_type, param = action['type'], action['param']
        try:
            if action_type == 'open_program':
                os.startfile(param)
            else:
                # Solange Strg/Alt/… noch gedrückt sind, würden getippte Zeichen
                # zu Tastenkürzeln im Zielprogramm
                self._wait_for_modifier_release()
                if action_type == 'type_text':
                    self._type_text(param)
                else:
                    self._send_combo(param)
        except TimeoutError:
            self.failed.emit(f'Hotkey {format_combo(combo)} abgebrochen: Strg/Alt/Umschalt/Win '
                             'wurde nicht losgelassen. Kombination kurz drücken und loslassen.')
        except OSError as exc:
            log.warning('Aktion für %s fehlgeschlagen: %s', combo, exc)
            self.failed.emit(f'Hotkey {format_combo(combo)}: „{param}“ ließ sich nicht öffnen '
                             f'({exc.strerror or exc}). Pfad über „Bearbeiten“ korrigieren.')
        except Exception as exc:  # Aktion darf den Worker nie beenden
            log.exception('Aktion für %s fehlgeschlagen', combo)
            self.failed.emit(f'Hotkey {format_combo(combo)} fehlgeschlagen: {exc}')
        else:
            self.finished.emit(f'Hotkey {format_combo(combo)} ausgeführt')

    def _wait_for_modifier_release(self):
        deadline = time.monotonic() + self.MODIFIER_RELEASE_TIMEOUT
        while any(is_key_down(vk) for vk in MODIFIER_VKS):
            if time.monotonic() > deadline:
                raise TimeoutError
            time.sleep(0.02)

    def _type_text(self, text):
        for char in text.replace('\r\n', '\n').replace('\r', '\n'):
            self._keyboard.type(char)  # pynput sendet '\n' als Enter
            time.sleep(self.TYPE_DELAY)

    @staticmethod
    def _send_combo(combo):
        vks = [NAME_TO_VK[key] for key in normalize_combo(combo).split('+')]

        def send(vk, up):
            flags = (win32con.KEYEVENTF_EXTENDEDKEY if vk in EXTENDED_VKS else 0) \
                | (win32con.KEYEVENTF_KEYUP if up else 0)
            win32api.keybd_event(vk, 0, flags, 0)

        for vk in vks:
            send(vk, up=False)
        time.sleep(0.02)
        for vk in reversed(vks):
            send(vk, up=True)


# --- Globaler Tastatur-Hook --------------------------------------------------

class HotkeyListener:
    """Erkennt registrierte Kombinationen systemweit und ruft on_trigger(combo) auf."""

    def __init__(self, on_trigger):
        self._on_trigger = on_trigger
        self._combos = {}   # frozenset der Tastennamen -> Kombination als Text
        self._pressed = {}  # Tastenname -> vk der gehaltenen Taste
        self._lock = threading.Lock()
        self._listener = None

    def set_hotkeys(self, combos):
        # Referenz-Tausch ist atomar; der Hook-Thread sieht alt oder neu, nie halb
        self._combos = {frozenset(combo.split('+')): combo for combo in combos}

    def start(self):
        if self._listener is None:
            self._listener = pynput_keyboard.Listener(on_press=self._on_press,
                                                      on_release=self._on_release)
            self._listener.start()

    def stop(self):
        if self._listener is not None:
            self._listener.stop()
            self._listener = None

    @staticmethod
    def _key_name(key):
        if isinstance(key, pynput_keyboard.Key):
            key = key.value
        return VK_TO_NAME.get(getattr(key, 'vk', None))

    def _on_press(self, key, injected):
        # Eigene simulierte Eingaben ignorieren – sonst könnte eine gesendete
        # Kombination sich selbst wieder auslösen
        name = None if injected else self._key_name(key)
        if name is None:
            return
        with self._lock:
            # Tasten verwerfen, deren Loslassen verpasst wurde (z. B. Win+L,
            # Wechsel zu einem Admin-Fenster). Die aktuelle Taste gilt im Hook
            # noch als "oben" – steht sie trotzdem als gedrückt da, ist es Auto-Repeat.
            self._pressed = {n: vk for n, vk in self._pressed.items() if is_key_down(vk)}
            if name in self._pressed:
                return
            self._pressed[name] = self._vk(key)
            combo = self._combos.get(frozenset(self._pressed))
        if combo:
            self._on_trigger(combo)

    def _on_release(self, key, injected):
        name = None if injected else self._key_name(key)
        if name is not None:
            with self._lock:
                self._pressed.pop(name, None)

    @staticmethod
    def _vk(key):
        return (key.value if isinstance(key, pynput_keyboard.Key) else key).vk


# --- Konfiguration -----------------------------------------------------------

class ConfigManager:
    """Liest und schreibt die Hotkeys (Pfad aus Kompatibilität zu 1.4.x unverändert)."""

    def __init__(self, path=None):
        self.path = Path(path) if path else Path.home() / '.productivity_hub' / 'config.json'
        self.load_warning = ''
        self._data = {}      # übrige Felder der Datei bleiben beim Speichern erhalten
        self._invalid = {}   # unlesbare Einträge nicht stillschweigend verwerfen
        self.hotkeys = self._load()

    def _load(self):
        try:
            with open(self.path, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            log.warning('Konfiguration nicht lesbar: %s', exc)
            backup = self.path.with_name(self.path.stem + '.defekt.json')
            try:
                os.replace(self.path, backup)
                self.load_warning = (f'Die Konfigurationsdatei war beschädigt und wurde als '
                                     f'„{backup}“ gesichert. Es wird mit einer leeren Liste gestartet.')
            except OSError:
                self.load_warning = (f'Die Konfigurationsdatei „{self.path}“ ist nicht lesbar. '
                                     'Bitte Zugriffsrechte prüfen.')
            return {}

        self._data = data if isinstance(data, dict) else {}
        raw = self._data.get('hotkeys')
        hotkeys = {}
        for combo, entry in (raw.items() if isinstance(raw, dict) else ()):
            try:
                action = entry['action']
                hotkeys[validate_trigger(combo)] = {
                    'action': {'type': action['type'],
                               'param': validate_action(action['type'], action['param'])},
                    'description': str(entry.get('description', '')),
                }
            except (ValueError, KeyError, TypeError, AttributeError):
                self._invalid[combo] = entry
        if self._invalid:
            self.load_warning = (f'{len(self._invalid)} Eintrag/Einträge in „{self.path}“ '
                                 f'sind ungültig und bleiben inaktiv: {", ".join(self._invalid)}. '
                                 'Bitte neu anlegen oder in der Datei korrigieren.')
        return hotkeys

    def save(self):
        """Schreibt atomar (erst Temp-Datei, dann ersetzen). Wirft OSError."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {**self._data, 'hotkeys': {**self._invalid, **self.hotkeys}}
        tmp = self.path.with_name(self.path.name + '.tmp')
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, self.path)


# --- Oberfläche --------------------------------------------------------------

STYLESHEET = """
QWidget { background: #23272e; color: #e0e0e0;
          font-family: 'Segoe UI', Arial, sans-serif; font-size: 15px; }
QGroupBox { border: 1px solid #444; border-radius: 10px; margin-top: 14px;
            padding: 12px 10px 10px 10px; background: #282c34; }
QGroupBox::title { subcontrol-origin: margin; left: 12px; padding: 0 4px; }
QTabWidget::pane { border: 1px solid #444; border-radius: 8px; }
QTabBar::tab { background: #282c34; border: 1px solid #444; padding: 8px 20px;
               border-top-left-radius: 8px; border-top-right-radius: 8px; margin-right: 2px; }
QTabBar::tab:selected { background: #1565c0; color: #fff; }
QLineEdit, QComboBox, QTextBrowser, QListWidget {
    background: #1d2026; border: 1px solid #555; border-radius: 6px; padding: 6px; }
QLineEdit:focus, QComboBox:focus, QListWidget:focus, QTextBrowser:focus { border: 2px solid #ffd54f; }
QLineEdit[readOnly="true"] { background: #2b2f36; }
QListWidget::item { padding: 6px; }
QListWidget::item:selected { background: #1565c0; color: #fff; }
QPushButton { background: #3a3f4b; color: #fff; border: 1px solid #555;
              border-radius: 6px; padding: 7px 16px; }
QPushButton:hover { background: #50576a; }
QPushButton:focus { border: 2px solid #ffd54f; }
QPushButton:disabled { background: #2b2f36; color: #9aa0aa; border-color: #444; }
QPushButton#primary { background: #2e7d32; }
QPushButton#primary:hover { background: #1b5e20; }
QPushButton#select { background: #1565c0; }
QPushButton#select:hover { background: #0d47a1; }
QPushButton#danger { background: #ffa726; color: #1a1a1a; }
QPushButton#danger:hover { background: #ffb74d; }
/* muss nach den Farbrollen stehen, sonst sehen deaktivierte Buttons aktiv aus */
QPushButton#primary:disabled, QPushButton#select:disabled, QPushButton#danger:disabled {
    background: #2b2f36; color: #9aa0aa; border-color: #444; }
QPushButton#key { min-width: 38px; min-height: 30px; padding: 4px 6px; }
QPushButton#key:checked { background: #2e7d32; font-weight: bold; }
QLabel { background: transparent; }
QLabel#hint { color: #b0b6c0; font-size: 13px; }
QStatusBar { color: #c8ccd4; border-top: 1px solid #444; }
"""


def styled_button(text, role=None, tooltip=''):
    btn = QPushButton(text)
    if role:
        btn.setObjectName(role)
    if tooltip:
        btn.setToolTip(tooltip)
    return btn


class VirtualKeyboard(QDialog):
    """Bildschirm-Tastatur zum Zusammenklicken einer Tastenkombination."""

    def __init__(self, parent=None, preset=''):
        super().__init__(parent)
        self.result_combo = ''
        self._buttons = {}
        self._init_ui()
        for key in (preset.split('+') if preset else []):
            if key in self._buttons:
                self._buttons[key].setChecked(True)
        self._update_label()

    def _init_ui(self):
        self.setWindowTitle('Tastenkombination wählen')
        self.setMinimumSize(760, 520)
        layout = QVBoxLayout(self)

        title = QLabel('Tasten anklicken, die gleichzeitig gedrückt werden sollen:')
        title.setStyleSheet('font-weight: bold; font-size: 16px;')
        layout.addWidget(title)
        hint = QLabel('Mindestens eine normale Taste plus Strg, Alt oder Win – '
                      'F-Tasten und Pause gehen auch allein.')
        hint.setObjectName('hint')
        hint.setWordWrap(True)
        layout.addWidget(hint)

        for row in KEYBOARD_ROWS:
            grid = QGridLayout()
            for col, (label, key) in enumerate(row):
                btn = styled_button(label, 'key')
                btn.setCheckable(True)
                btn.setAccessibleName(f'Taste {label}')
                btn.toggled.connect(self._update_label)
                self._buttons[key] = btn
                grid.addWidget(btn, 0, col)
            layout.addLayout(grid)

        self.combo_label = QLabel()
        self.combo_label.setStyleSheet('font-weight: bold; color: #90caf9;')
        self.combo_label.setAccessibleName('Ausgewählte Kombination')
        layout.addWidget(self.combo_label)

        buttons = QHBoxLayout()
        clear_btn = styled_button('Auswahl leeren', 'danger')
        clear_btn.clicked.connect(self._clear)
        cancel_btn = styled_button('Abbrechen')
        cancel_btn.clicked.connect(self.reject)
        ok_btn = styled_button('Übernehmen', 'primary')
        ok_btn.setDefault(True)
        ok_btn.clicked.connect(self._apply)
        buttons.addWidget(clear_btn)
        buttons.addStretch()
        buttons.addWidget(cancel_btn)
        buttons.addWidget(ok_btn)
        layout.addLayout(buttons)

    def _selected_combo(self):
        return '+'.join(key for key, btn in self._buttons.items() if btn.isChecked())

    def _update_label(self):
        combo = self._selected_combo()
        shown = format_combo(normalize_combo(combo)) if combo else '(keine)'
        self.combo_label.setText(f'Ausgewählt: {shown}')

    def _clear(self):
        for btn in self._buttons.values():
            btn.setChecked(False)

    def _apply(self):
        try:
            self.result_combo = validate_trigger(self._selected_combo())
        except ValueError as exc:
            QMessageBox.warning(self, 'Kombination nicht geeignet', str(exc))
            return
        self.accept()


class MainWindow(QMainWindow):
    trigger = pyqtSignal(str)  # vom Hook-Thread in den GUI-Thread

    def __init__(self):
        super().__init__()
        self.config = ConfigManager()
        self.runner = ActionRunner()
        self.listener = HotkeyListener(self.trigger.emit)
        self._editing = None  # Kombination des gerade bearbeiteten Eintrags
        self._combo = ''      # gewählte Kombination; das Feld zeigt sie in deutscher Schreibweise

        self.trigger.connect(self._on_trigger)
        self.runner.finished.connect(lambda msg: self.statusBar().showMessage(msg, 3000))
        self.runner.failed.connect(self._on_action_failed)

        self._init_ui()
        self._refresh()
        self.listener.start()

        if self.config.load_warning:
            QMessageBox.warning(self, 'Konfiguration', self.config.load_warning)

    # Aufbau ------------------------------------------------------------------

    def _init_ui(self):
        self.setWindowTitle(f'{APP_NAME} {APP_VERSION}')
        self.setMinimumSize(720, 600)
        tabs = QTabWidget()
        tabs.addTab(self._create_hotkeys_tab(), 'Hotkeys')
        tabs.addTab(self._create_info_tab(), 'Info')
        self.setCentralWidget(tabs)
        self.statusBar().showMessage('Bereit – Hotkeys sind aktiv, solange das Programm läuft.')

    def _create_hotkeys_tab(self):
        widget = QWidget()
        layout = QVBoxLayout(widget)

        list_group = QGroupBox('Registrierte Hotkeys')
        list_layout = QVBoxLayout(list_group)
        self.hotkey_list = QListWidget()
        self.hotkey_list.setAccessibleName('Registrierte Hotkeys')
        self.hotkey_list.itemDoubleClicked.connect(lambda _: self._edit_selected())
        self.hotkey_list.currentItemChanged.connect(lambda *_: self._update_list_buttons())
        list_layout.addWidget(self.hotkey_list)
        self.empty_hint = QLabel('Noch keine Hotkeys – unten den ersten anlegen.')
        self.empty_hint.setObjectName('hint')
        list_layout.addWidget(self.empty_hint)
        list_buttons = QHBoxLayout()
        self.edit_btn = styled_button('✎ Bearbeiten', 'select')
        self.edit_btn.clicked.connect(self._edit_selected)
        self.delete_btn = styled_button('🗑 Löschen', 'danger')
        self.delete_btn.clicked.connect(self._delete_selected)
        list_buttons.addWidget(self.edit_btn)
        list_buttons.addWidget(self.delete_btn)
        list_buttons.addStretch()
        list_layout.addLayout(list_buttons)
        layout.addWidget(list_group, stretch=1)

        self.form_group = QGroupBox()
        form = QGridLayout(self.form_group)
        form.setColumnStretch(1, 1)

        self.combo_input = QLineEdit()
        self.combo_input.setReadOnly(True)
        self.combo_input.setPlaceholderText('Hier klicken, um die Tasten zu wählen')
        self.combo_input.setToolTip('Öffnet die Bildschirm-Tastatur')
        self.combo_input.setCursor(Qt.CursorShape.PointingHandCursor)
        self.combo_input.installEventFilter(self)  # Klick ins Feld öffnet die Tastatur
        keyboard_btn = styled_button('Tasten wählen …', 'select', 'Öffnet die Bildschirm-Tastatur')
        keyboard_btn.clicked.connect(self._open_virtual_keyboard)
        self._add_form_row(form, 0, '&Tastenkombination:', self.combo_input, keyboard_btn)

        self.action_combo = QComboBox()
        for action_type, action in ACTIONS.items():
            self.action_combo.addItem(action['name'], action_type)
        self.action_combo.currentIndexChanged.connect(self._update_param_input)
        self._add_form_row(form, 1, '&Aktion:', self.action_combo)

        self.param_input = QLineEdit()
        self.browse_btn = styled_button('Durchsuchen …', 'select', 'Programm oder Datei auswählen')
        self.browse_btn.clicked.connect(self._browse_program)
        self.param_label = self._add_form_row(form, 2, '', self.param_input, self.browse_btn)
        self.param_hint = QLabel()
        self.param_hint.setObjectName('hint')
        self.param_hint.setWordWrap(True)
        form.addWidget(self.param_hint, 3, 1)

        self.desc_input = QLineEdit()
        self.desc_input.setPlaceholderText('Optional – erscheint in der Liste, z. B. „E-Mail-Signatur“')
        self._add_form_row(form, 4, '&Beschreibung:', self.desc_input)

        form_buttons = QHBoxLayout()
        self.save_btn = styled_button('', 'primary')
        self.save_btn.clicked.connect(self._save_hotkey)
        self.cancel_edit_btn = styled_button('Bearbeiten abbrechen', tooltip='Formular leeren, nichts ändern')
        self.cancel_edit_btn.clicked.connect(self._reset_form)
        form_buttons.addStretch()
        form_buttons.addWidget(self.cancel_edit_btn)
        form_buttons.addWidget(self.save_btn)
        form.addLayout(form_buttons, 5, 0, 1, 2)
        layout.addWidget(self.form_group)

        self._reset_form()
        return widget

    @staticmethod
    def _add_form_row(grid, row, label_text, field, extra=None):
        label = QLabel(label_text)
        label.setBuddy(field)  # verknüpft Beschriftung und Feld (Screenreader, Alt+Buchstabe)
        field.setAccessibleName(label_text.replace('&', '').rstrip(':'))
        grid.addWidget(label, row, 0)
        if extra is None:
            grid.addWidget(field, row, 1)
        else:
            # Feld und Zusatz-Button teilen sich eine Zelle; ist der Button
            # ausgeblendet, nimmt das Feld die volle Breite ein
            cell = QHBoxLayout()
            cell.addWidget(field, stretch=1)
            cell.addWidget(extra)
            grid.addLayout(cell, row, 1)
        return label

    def eventFilter(self, obj, event):
        if obj is self.combo_input and event.type() == QEvent.Type.MouseButtonRelease:
            self._open_virtual_keyboard()
            return True
        return super().eventFilter(obj, event)

    def _create_info_tab(self):
        info = QTextBrowser()
        info.setOpenExternalLinks(False)
        info.setHtml(f"""
        <h2>{APP_NAME} {APP_VERSION}</h2>
        <p>Systemweite Tastenkürzel für Windows.<br>
           Autor: {APP_AUTHOR} – Wolfram Consult GmbH &amp; Co. KG</p>
        <p>Open Source unter der <b>Apache-Lizenz 2.0</b>: Nutzung, Änderung und
           Weitergabe sind erlaubt, auch kommerziell. Bedingung: Lizenztext und
           Urheberhinweis (Dateien <code>LICENSE</code> und <code>NOTICE</code>)
           bleiben erhalten. Keine Gewährleistung.</p>
        <h3>Aktionen</h3>
        <ul>
          <li><b>Text eingeben:</b> tippt den Text in das gerade aktive Fenster
              (funktioniert auch in Passwortfeldern).</li>
          <li><b>Programm/Datei öffnen:</b> startet ein Programm oder öffnet eine Datei
              bzw. einen Ordner – sofort beim Drücken des Hotkeys.</li>
          <li><b>Tastenkombination senden:</b> simuliert andere Tasten, z. B. <code>ctrl+s</code>.</li>
        </ul>
        <p>„Text eingeben“ und „Tastenkombination senden“ starten erst, wenn
           Strg/Alt/Umschalt/Win losgelassen sind – sonst würden die Zeichen zu
           Tastenkürzeln. Wird nicht innerhalb von 3 Sekunden losgelassen, bricht die
           Aktion ab (Meldung in der Statusleiste).</p>
        <h3>Hotkey festlegen</h3>
        <ul>
          <li>Die Kombination wird über „Tasten wählen …“ zusammengeklickt.</li>
          <li>Nötig ist mindestens eine normale Taste plus <b>Strg, Alt oder Win</b>.
              Umschalt allein reicht nicht, weil damit normal geschrieben wird.
              F-Tasten und Pause funktionieren auch ohne Zusatztaste.</li>
          <li>Ziffern zählen nur in der oberen Zahlenreihe, nicht im Ziffernblock.</li>
          <li>Auf deutschen Tastaturen ist <b>AltGr</b> dasselbe wie Strg+Alt.
              Kombinationen wie Strg+Alt+E (€) oder Strg+Alt+Q (@)
              daher meiden – das Zeichen würde zusätzlich getippt.</li>
          <li>Die Tasten des Hotkeys erreichen auch das aktive Programm. Am besten
              Kombinationen wählen, die dort nichts anderes auslösen.</li>
        </ul>
        <h3>Tastennamen für „Tastenkombination senden“</h3>
        <p>Mit <code>+</code> verbinden, Groß-/Kleinschreibung egal:
           <code>ctrl</code>, <code>alt</code>, <code>shift</code>, <code>win</code>,
           <code>a</code>–<code>z</code>, <code>0</code>–<code>9</code>, <code>f1</code>–<code>f24</code>,
           <code>space</code>, <code>enter</code>, <code>tab</code>, <code>esc</code>,
           <code>backspace</code>, <code>delete</code>, <code>insert</code>, <code>home</code>,
           <code>end</code>, <code>pageup</code>, <code>pagedown</code>,
           <code>left</code>, <code>up</code>, <code>right</code>, <code>down</code>,
           <code>printscreen</code>, <code>pause</code>.</p>
        <h3>Beispiele</h3>
        <ul>
          <li>Strg + Alt + N → Text eingeben: „name@firma.de“</li>
          <li>Strg + Alt + C → Programm öffnen: „calc.exe“</li>
          <li>F9 → Tastenkombination senden: <code>ctrl+s</code> (Speichern)</li>
        </ul>
        <h3>Hinweise</h3>
        <ul>
          <li>Hotkeys wirken nur, solange {APP_NAME} läuft – minimiert ist in Ordnung,
              Schließen des Fensters beendet sie.</li>
          <li>In Fenstern, die als Administrator laufen, greifen Hotkeys nur,
              wenn auch {APP_NAME} als Administrator gestartet wurde.</li>
          <li>Gespeichert wird in <code>{self.config.path}</code>.</li>
        </ul>
        """)
        return info

    # Liste -------------------------------------------------------------------

    def _refresh(self, select=None):
        self.hotkey_list.clear()
        for combo, entry in sorted(self.config.hotkeys.items()):
            action = entry['action']
            label = ACTIONS[action['type']]['name']
            # Text-Inhalte nicht anzeigen – es können Passwörter sein
            detail = '' if action['type'] == 'type_text' else f': {action["param"]}'
            text = f'{format_combo(combo)}   →   {label}{detail}'
            if entry['description']:
                text += f'   ({entry["description"]})'
            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, combo)
            self.hotkey_list.addItem(item)
            if combo == select:
                self.hotkey_list.setCurrentItem(item)
        self.empty_hint.setVisible(not self.config.hotkeys)
        self.listener.set_hotkeys(self.config.hotkeys)
        self._update_list_buttons()

    def _selected_combo(self):
        item = self.hotkey_list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def _update_list_buttons(self):
        has_selection = self._selected_combo() is not None
        for btn, tooltip in ((self.edit_btn, 'Gewählten Hotkey unten zum Ändern laden (auch per Doppelklick)'),
                             (self.delete_btn, 'Gewählten Hotkey entfernen')):
            btn.setEnabled(has_selection)
            btn.setToolTip(tooltip if has_selection else 'Erst einen Hotkey in der Liste anklicken')

    def _edit_selected(self):
        combo = self._selected_combo()
        if combo is None:
            return
        entry = self.config.hotkeys[combo]
        self._editing = combo
        self._set_combo(combo)
        self.action_combo.setCurrentIndex(self.action_combo.findData(entry['action']['type']))
        self.param_input.setText(entry['action']['param'])
        self.desc_input.setText(entry['description'])
        self._update_form_mode()
        self.param_input.setFocus()

    def _delete_selected(self):
        combo = self._selected_combo()
        if combo is None:
            return
        answer = QMessageBox.question(self, 'Hotkey löschen',
                                      f'Hotkey „{format_combo(combo)}“ wirklich löschen?')
        if answer != QMessageBox.StandardButton.Yes:
            return
        entry = self.config.hotkeys.pop(combo)
        if not self._persist():
            self.config.hotkeys[combo] = entry
            return
        if self._editing == combo:
            self._reset_form()
        self._refresh()
        self.statusBar().showMessage(f'Hotkey {format_combo(combo)} gelöscht', 3000)

    # Formular ----------------------------------------------------------------

    def _set_combo(self, combo):
        self._combo = combo
        self.combo_input.setText(format_combo(combo) if combo else '')

    def _reset_form(self):
        self._editing = None
        self._set_combo('')
        self.param_input.clear()
        self.desc_input.clear()
        self.action_combo.setCurrentIndex(0)
        self._update_param_input()
        self._update_form_mode()

    def _update_form_mode(self):
        editing = self._editing is not None
        self.form_group.setTitle(f'Hotkey „{format_combo(self._editing)}“ bearbeiten' if editing
                                 else 'Neuen Hotkey anlegen')
        self.save_btn.setText('Änderungen speichern' if editing else 'Hinzufügen')
        self.cancel_edit_btn.setVisible(editing)

    def _update_param_input(self):
        action_type = self.action_combo.currentData()
        action = ACTIONS[action_type]
        self.browse_btn.setVisible(action_type == 'open_program')
        self.param_label.setText(action['field'])
        self.param_input.setAccessibleName(action['field'].replace('&', '').rstrip(':'))
        self.param_input.setPlaceholderText(action['placeholder'])
        self.param_hint.setText(action['hint'])

    def _open_virtual_keyboard(self):
        dialog = VirtualKeyboard(self, preset=self._combo)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._set_combo(dialog.result_combo)

    def _browse_program(self):
        path, _ = QFileDialog.getOpenFileName(self, 'Programm oder Datei auswählen', '',
                                              'Programme (*.exe *.bat *.cmd *.lnk);;Alle Dateien (*)')
        if path:
            self.param_input.setText(os.path.normpath(path))

    def _save_hotkey(self):
        action_type = self.action_combo.currentData()
        try:
            if not self._combo:
                raise ValueError('Bitte zuerst über „Tasten wählen …“ eine Tastenkombination festlegen.')
            combo = validate_trigger(self._combo)
            param = validate_action(action_type, self.param_input.text())
        except ValueError as exc:
            QMessageBox.warning(self, 'Eingabe unvollständig', str(exc))
            return

        if combo != self._editing and combo in self.config.hotkeys:
            answer = QMessageBox.question(
                self, 'Kombination schon belegt',
                f'„{format_combo(combo)}“ ist bereits vergeben. Den bestehenden Hotkey ersetzen?')
            if answer != QMessageBox.StandardButton.Yes:
                return

        backup = dict(self.config.hotkeys)
        if self._editing is not None:
            self.config.hotkeys.pop(self._editing, None)
        self.config.hotkeys[combo] = {
            'action': {'type': action_type, 'param': param},
            'description': self.desc_input.text().strip(),
        }
        if not self._persist():
            self.config.hotkeys = backup
            return
        verb = 'geändert' if self._editing is not None else 'hinzugefügt'
        self._reset_form()
        self._refresh(select=combo)
        self.statusBar().showMessage(f'Hotkey {format_combo(combo)} {verb}', 3000)

    def _persist(self):
        try:
            self.config.save()
            return True
        except OSError as exc:
            QMessageBox.critical(self, 'Speichern fehlgeschlagen',
                                 f'„{self.config.path}“ konnte nicht geschrieben werden ({exc}). '
                                 'Bitte Speicherplatz und Zugriffsrechte prüfen – die Änderung '
                                 'wurde nicht übernommen.')
            return False

    # Ausführung --------------------------------------------------------------

    def _on_trigger(self, combo):
        entry = self.config.hotkeys.get(combo)
        if entry:
            self.runner.submit(combo, dict(entry['action']))

    def _on_action_failed(self, message):
        self.statusBar().showMessage(message, 10000)
        if self.isMinimized() or not self.isActiveWindow():
            QApplication.beep()

    def closeEvent(self, event):
        self.listener.stop()
        self.runner.shutdown()
        event.accept()


def main():
    logging.basicConfig(level=logging.WARNING, format='%(levelname)s %(name)s: %(message)s')
    app = QApplication(sys.argv)
    app.setStyle('Fusion')
    app.setStyleSheet(STYLESHEET)
    icon_path = resource_path('icon.ico')
    if os.path.exists(icon_path):
        app.setWindowIcon(QIcon(icon_path))

    # Zwei Instanzen würden jeden Hotkey doppelt ausführen
    lock = QLockFile(os.path.join(QDir.tempPath(), 'hotkey-master.lock'))
    if not lock.tryLock(100):
        QMessageBox.information(None, APP_NAME,
                                f'{APP_NAME} läuft bereits. Bitte das vorhandene Fenster '
                                'in der Taskleiste verwenden.')
        return 0

    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == '__main__':
    sys.exit(main())
