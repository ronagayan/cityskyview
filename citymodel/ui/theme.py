"""Dark theme."""

from __future__ import annotations

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

BG = "#14171c"
PANEL = "#1b1f26"
CARD = "#222730"
LINE = "#323946"
TEXT = "#e3e7ee"
MUTED = "#8a93a1"
ACCENT = "#ffb454"
ACCENT_HOVER = "#ffc57a"
DANGER = "#ff6a55"
OK = "#6fcf97"

QSS = f"""
* {{ font-family: "Segoe UI", "Inter", sans-serif; font-size: 10pt; color: {TEXT}; }}
QMainWindow, QDialog {{ background: {BG}; }}
QWidget#sidebar, QWidget#sidebarInner {{ background: {PANEL}; }}
QScrollArea {{ border: none; background: {PANEL}; }}
QSplitter::handle {{ background: {BG}; width: 6px; height: 6px; }}
QSplitter::handle:hover {{ background: {LINE}; }}
QFrame#panel {{ background: {PANEL}; border: 1px solid {LINE}; border-radius: 10px; }}
QLabel#panelTitle {{ font-size: 11pt; font-weight: 600; }}
QLabel#muted, QLabel#hint {{ color: {MUTED}; }}
QLabel#hint {{ font-size: 9pt; }}
QLabel#placeholder {{ color: {MUTED}; font-size: 11pt; background: {BG}; }}
QLabel#stale {{ background: #3a2f1c; color: {ACCENT}; border: 1px solid #5a4520;
               border-radius: 6px; padding: 5px 8px; }}
QLabel#appTitle {{ font-size: 14pt; font-weight: 700; }}

QGroupBox {{ background: {CARD}; border: 1px solid {LINE}; border-radius: 10px;
            margin-top: 14px; padding: 12px 10px 10px 10px; font-weight: 600; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 12px; top: 2px; padding: 0 4px;
                   color: {ACCENT}; }}

QLineEdit, QDoubleSpinBox, QSpinBox, QComboBox, QPlainTextEdit, QListWidget {{
    background: {BG}; border: 1px solid {LINE}; border-radius: 6px; padding: 5px 7px;
    selection-background-color: {ACCENT}; selection-color: #1a1300; }}
QLineEdit:focus, QDoubleSpinBox:focus, QSpinBox:focus, QComboBox:focus {{ border-color: {ACCENT}; }}
QComboBox::drop-down {{ border: none; width: 22px; }}
QComboBox QAbstractItemView {{ background: {CARD}; border: 1px solid {LINE};
                              selection-background-color: {ACCENT}; selection-color: #1a1300; }}
QDoubleSpinBox::up-button, QDoubleSpinBox::down-button, QSpinBox::up-button, QSpinBox::down-button {{
    width: 16px; border: none; background: transparent; }}
QListWidget::item {{ padding: 4px 6px; border-radius: 4px; }}
QListWidget::item:selected {{ background: {LINE}; color: {TEXT}; }}
QListWidget::item:hover {{ background: #2a303b; }}

QPushButton, QToolButton {{ background: {CARD}; border: 1px solid {LINE}; border-radius: 7px;
                           padding: 6px 12px; }}
QPushButton:hover, QToolButton:hover {{ background: #2c333f; border-color: #46505f; }}
QPushButton:pressed, QToolButton:pressed {{ background: {LINE}; }}
QPushButton:disabled, QToolButton:disabled {{ color: #5d6572; background: #1d2128; border-color: #272c35; }}
QPushButton#primary {{ background: {ACCENT}; color: #1a1300; border: none; font-weight: 700;
                      padding: 9px 16px; }}
QPushButton#primary:hover {{ background: {ACCENT_HOVER}; }}
QPushButton#primary:disabled {{ background: #5a4a30; color: #2a2210; }}
QPushButton#danger {{ color: {DANGER}; }}

QCheckBox {{ spacing: 8px; }}
QCheckBox::indicator {{ width: 16px; height: 16px; border-radius: 4px; border: 1px solid #4a5361;
                       background: {BG}; }}
QCheckBox::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT}; image: none; }}
QCheckBox::indicator:hover {{ border-color: {ACCENT}; }}

QProgressBar {{ background: {BG}; border: 1px solid {LINE}; border-radius: 6px; height: 14px;
               text-align: center; font-size: 8pt; }}
QProgressBar::chunk {{ background: {ACCENT}; border-radius: 5px; }}
QStatusBar {{ background: {PANEL}; border-top: 1px solid {LINE}; }}
QStatusBar::item {{ border: none; }}
QToolTip {{ background: {CARD}; color: {TEXT}; border: 1px solid {LINE}; padding: 5px; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: #3a4250; border-radius: 4px; min-height: 30px; }}
QScrollBar::handle:vertical:hover {{ background: #4a5464; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: #3a4250; border-radius: 4px; min-width: 30px; }}
QPlainTextEdit#log {{ font-family: "Cascadia Mono", "Consolas", monospace; font-size: 9pt;
                     background: #0f1115; color: #b8c0cc; }}
QMenu {{ background: {CARD}; border: 1px solid {LINE}; padding: 4px; }}
QMenu::item {{ padding: 6px 18px; border-radius: 4px; }}
QMenu::item:selected {{ background: {LINE}; }}
QMessageBox {{ background: {PANEL}; }}
"""


def apply(app: QApplication):
    app.setStyle("Fusion")
    pal = QPalette()
    for role, color in ((QPalette.ColorRole.Window, BG), (QPalette.ColorRole.Base, BG),
                        (QPalette.ColorRole.AlternateBase, PANEL),
                        (QPalette.ColorRole.Button, CARD), (QPalette.ColorRole.Text, TEXT),
                        (QPalette.ColorRole.WindowText, TEXT), (QPalette.ColorRole.ButtonText, TEXT),
                        (QPalette.ColorRole.ToolTipBase, CARD), (QPalette.ColorRole.ToolTipText, TEXT),
                        (QPalette.ColorRole.Highlight, ACCENT),
                        (QPalette.ColorRole.HighlightedText, "#1a1300"),
                        (QPalette.ColorRole.PlaceholderText, MUTED)):
        pal.setColor(role, QColor(color))
    app.setPalette(pal)
    app.setStyleSheet(QSS)
