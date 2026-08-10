import sys
import os
from PyQt6.QtWidgets import (QApplication, QMainWindow, QVBoxLayout, QHBoxLayout, QGridLayout, 
                             QWidget, QLabel, QLineEdit, QPushButton, QTableView, QTableWidget,
                             QComboBox, QMessageBox, QHeaderView, QTabWidget, QInputDialog,
                             QCheckBox, QScrollArea, QFileDialog, QDialog, QAbstractItemView,
                             QFrame, QSizePolicy, QListWidget, QGraphicsDropShadowEffect, QTableWidgetItem,
                             QTextBrowser, QSpinBox)
from PyQt6.QtSql import QSqlDatabase, QSqlTableModel, QSqlQuery
from PyQt6.QtGui import QPixmap, QFont, QMovie, QDesktopServices, QColor
from PyQt6.QtCore import Qt, QSize, QUrl, QTimer
import pandas as pd
import matplotlib.pyplot as plt
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
import sqlite3
import shutil
import json

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


    def __init__(self, columns, default_headers, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Экспорти маълумот ба Excel")
        self.resize(400, 500)
        self.layout = QVBoxLayout(self)

        self.label = QLabel("Устунҳоро барои экспорт интихоб кунед:")
        self.layout.addWidget(self.label)

        # Select All Checkbox
        self.select_all_cb = QCheckBox("Ҳамаро интихоб кардан")
        self.layout.addWidget(self.select_all_cb)

        # Column Checkboxes layout inside a scroll area
        self.scroll_area = QScrollArea()
        self.scroll_widget = QWidget()
        self.scroll_layout = QVBoxLayout(self.scroll_widget)
        
        self.checkboxes = {}
        for col in columns:
            if col == 'id':
                continue
            display_name = default_headers.get(col, col)
            cb = QCheckBox(display_name)
            cb.setChecked(True)  # default checked
            self.scroll_layout.addWidget(cb)
            self.checkboxes[col] = cb

        self.scroll_widget.setLayout(self.scroll_layout)
        self.scroll_area.setWidget(self.scroll_widget)
        self.scroll_area.setWidgetResizable(True)
        self.layout.addWidget(self.scroll_area)

        # Connect select all
        self.select_all_cb.setChecked(True)
        self.select_all_cb.stateChanged.connect(self.toggle_all)

        # Buttons
        self.btn_layout = QHBoxLayout()
        self.btn_export = QPushButton("Экспорт ба Excel")
        self.btn_export.setObjectName("btnExecuteExport")
        self.btn_export.clicked.connect(self.accept)
        
        self.btn_cancel = QPushButton("Бекор кардан")
        self.btn_cancel.clicked.connect(self.reject)

        self.btn_layout.addWidget(self.btn_export)
        self.btn_layout.addWidget(self.btn_cancel)
        self.layout.addLayout(self.btn_layout)

    def toggle_all(self, state):
        is_checked = (state == 2)  # Qt.CheckState.Checked
        for cb in self.checkboxes.values():
            cb.setChecked(is_checked)

    def get_selected_columns(self):
        return [col for col, cb in self.checkboxes.items() if cb.isChecked()]


class ExcelTableView(QTableView):
    def __init__(self, parent=None):
        super().__init__(parent)

    def keyPressEvent(self, event):
        # 1. Execute the default table behavior FIRST.
        # This guarantees PyQt fully commits active cell edits to SQLite and closes the editor safely first.
        super().keyPressEvent(event)

        # 2. After the default event has safely finished and saved, handle the Enter key to move focus down
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            current = self.currentIndex()
            if current.isValid():
                next_row = current.row() + 1
                if next_row < self.model().rowCount():
                    next_index = self.model().index(next_row, current.column())
                    self.setCurrentIndex(next_index)
                    event.accept()



# ==========================================================
# МАЪЛУМОТҲОИ БАЗА БАРОИ ТАҲЛИЛ
# ==========================================================
class DatabaseManager:
    def __init__(self):
        data_dir = os.path.join(BASE_DIR, 'data')
        if not os.path.exists(data_dir):
            os.makedirs(data_dir)
        self.conn = sqlite3.connect(os.path.join(data_dir, 'school.db'))
        self.create_tables()

    def create_tables(self):
        cursor = self.conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS students (
                id TEXT PRIMARY KEY,
                full_name TEXT,
                class_name TEXT,
                school_name TEXT
            )
        """)
        try:
            cursor.execute("ALTER TABLE students ADD COLUMN school_name TEXT")
        except:
            pass
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS grades (
                student_id TEXT,
                subject TEXT,
                score REAL
            )
        """)
        self.conn.commit()

    def import_excel(self, folder_path):
        """Scan folder_path for school subdirectories, then read class Excel files inside each.

        Expected layout:
            folder_path/
                Лицей №1/
                    10a.xlsx
                    11a.xlsx
                Мактаб №2/
                    9a.xlsx

        Each Excel file must have at minimum:
            Column 0: 'Синф'         – class label (e.g. '11-А')
            Column 1: 'Ном ва насаб' – student full name
            Column 2+: subject score columns (numeric 1–10)
        """
        if not os.path.exists(folder_path):
            os.makedirs(folder_path)
            return False

        cursor = self.conn.cursor()

        # ── Wipe previous analytics data ────────────────────────────
        cursor.execute("DELETE FROM students")
        cursor.execute("DELETE FROM grades")
        self.conn.commit()

        # ── Accepted name variants for the student-name column ──────
        NAME_VARIANTS = ['Ном ва насаб', 'Ном  ва насаб', 'Номи хонанда',
                         'ФИО', 'Ф.И.О', 'full_name']

        imported_any = False

        # ── Walk: top-level subdirs = schools ────────────────────────
        try:
            entries = os.listdir(folder_path)
        except OSError:
            return False

        school_subdirs = [
            e for e in entries
            if os.path.isdir(os.path.join(folder_path, e))
        ]

        # Fallback: if no subdirectories, treat root files as a single school
        if not school_subdirs:
            school_subdirs_map = {os.path.basename(folder_path): folder_path}
        else:
            school_subdirs_map = {
                sd: os.path.join(folder_path, sd) for sd in school_subdirs
            }

        for school_name, school_dir in school_subdirs_map.items():
            # Collect Excel files for this school
            excel_files = [
                os.path.join(school_dir, f)
                for f in os.listdir(school_dir)
                if f.endswith(('.xlsx', '.xls')) and not f.startswith('~$')
            ]

            if not excel_files:
                continue

            # Ensure the school exists in the QSql schools table (best-effort)
            try:
                import sqlite3 as _sq3
                schools_conn = _sq3.connect(os.path.join(BASE_DIR, 'schools.db'))
                sc = schools_conn.cursor()
                sc.execute("SELECT id FROM schools WHERE name = ?", (school_name,))
                if sc.fetchone() is None:
                    sc.execute("INSERT INTO schools (name) VALUES (?)", (school_name,))
                    schools_conn.commit()
                schools_conn.close()
            except Exception:
                pass

            for path in excel_files:
                # Derive class name from filename (e.g. '10a.xlsx' → '10a')
                file_class = os.path.splitext(os.path.basename(path))[0]

                try:
                    df = pd.read_excel(path)
                    df.columns = [str(c).strip() for c in df.columns]

                    # ── Identify student-name column ─────────────────
                    name_col = None
                    for variant in NAME_VARIANTS:
                        if variant in df.columns:
                            name_col = variant
                            break
                    if name_col is None:
                        # Use second column as fallback
                        name_col = df.columns[1] if len(df.columns) > 1 else None
                    if name_col is None:
                        continue

                    # ── Identify class column ─────────────────────────
                    class_col = 'Синф' if 'Синф' in df.columns else None

                    # Drop rows with missing name
                    df = df.dropna(subset=[name_col])

                    for _, row in df.iterrows():
                        ism = str(row[name_col]).strip()

                        # Skip junk rows
                        if (not ism or ism.lower() in ('nan', 'none') or
                                'столбец' in ism.lower() or
                                'unnamed' in ism.lower()):
                            continue

                        # Determine class name: prefer the cell value, then filename
                        if class_col and pd.notna(row.get(class_col)):
                            sinf = str(row[class_col]).strip()
                            if sinf.lower() in ('nan', 'none', ''):
                                sinf = file_class
                        else:
                            sinf = file_class

                        # Standardize class name with regex (e.g., "7б" → "7-Б", "10a" → "10-А")
                        import re
                        raw_class = sinf.strip()
                        match = re.search(r'(\d+)\s*[-_]?\s*([a-zA-Zа-яА-ЯёЁ])', raw_class)
                        if match:
                            num = match.group(1)
                            let = match.group(2).upper()
                            # Translate Latin A/B/C to Cyrillic А/Б/В for absolute consistency
                            translation = {'A': 'А', 'B': 'Б', 'C': 'В', 'K': 'К', 'a': 'А', 'b': 'Б', 'c': 'В', 'k': 'К'}
                            let = translation.get(let, let)
                            sinf = f"{num}-{let}"

                        student_id = f"{school_name}__{sinf}__{ism}"

                        cursor.execute(
                            """INSERT OR REPLACE INTO students
                               (id, full_name, class_name, school_name)
                               VALUES (?, ?, ?, ?)""",
                            (student_id, ism, sinf, school_name)
                        )

                        # ── Parse subject columns (everything after fixed columns) ──
                        fixed_cols = {name_col, class_col} if class_col else {name_col}
                        
                        # Subject normalizer: typo and synonym resolution
                        subject_synonyms = {
                            'ОДХ': 'ОИХ',
                            'ОДХ\n': 'ОИХ',
                            'ОДХ ': 'ОИХ',
                            'ОИХ': 'ОИХ',
                            'ОИХ ': 'ОИХ',
                            'ОИХ\n': 'ОИХ',
                            'ОИХ  ': 'ОИХ',
                            'ОИХ\t': 'ОИХ',
                            'ОИҲ': 'ОИХ',
                            'OIX': 'ОИХ',
                            'OIX ': 'ОИХ',
                            'OIH': 'ОИХ',
                            'оих': 'ОИХ',
                            'одх': 'ОИХ',
                            'оиҳ': 'ОИХ',
                            'oix': 'ОИХ',
                            'odx': 'ОИХ',
                            'oih': 'ОИХ',
                        }
                        
                        def normalize_subject(sub):
                            if not isinstance(sub, str):
                                sub = str(sub)
                            # Strip whitespace, collapse multiple spaces
                            sub_clean = sub.strip().upper()
                            sub_clean = ' '.join(sub_clean.split())
                            # Return canonical name if found in synonyms dict
                            return subject_synonyms.get(sub_clean, sub_clean)
                        
                        for sub in df.columns:
                            if sub in fixed_cols:
                                continue
                            low = sub.lower()
                            if ('unnamed' in low or 'жами' in low or
                                    'рейтинг' in low or 'tartib' in low or
                                    '№' in sub or sub.strip() == ''):
                                continue

                            canonical = normalize_subject(sub)

                            val = row.get(sub)
                            if val is None or (hasattr(val, '__class__') and
                                               val.__class__.__name__ == 'float' and
                                               str(val) == 'nan'):
                                continue
                            if not pd.notna(val):
                                continue

                            try:
                                score = float(val)
                                if 1 <= score <= 10:
                                    cursor.execute(
                                        """INSERT OR REPLACE INTO grades
                                           (student_id, subject, score)
                                           VALUES (?, ?, ?)""",
                                        (student_id, canonical, score)
                                    )
                            except (ValueError, TypeError):
                                continue

                        imported_any = True

                except Exception as e:
                    print(f"[import_excel] Хато дар файл '{path}': {e}")
                    continue

        self.conn.commit()
        return imported_any

class ResponsiveImage(QLabel):
    def __init__(self):
        super().__init__()
        self.pixmap_original = None
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setMinimumSize(80, 100)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        self.setStyleSheet("border: 2px solid #E2E8F0; border-radius: 15px; background-color: #F8FAFC;")

    def setPixmapOriginal(self, pixmap):
        self.pixmap_original = pixmap
        self.update_image()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.update_image()

    def update_image(self):
        if self.pixmap_original and not self.pixmap_original.isNull():
            scaled = self.pixmap_original.scaled(
                self.size(), 
                Qt.AspectRatioMode.KeepAspectRatio, 
                Qt.TransformationMode.SmoothTransformation
            )
            super().setPixmap(scaled)
        else:
            super().setPixmap(QPixmap())

class SchoolManagementSystem(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Идоракунии маорифи Зафаробод")
        self.setStyleSheet(self.get_stylesheet())

        # Initialize core column translations
        self.default_headers = {
            "id": "ID",
            "name": "Номи муассаса",
            "director": "Директор",
            "phone": "Рақами телефон",
            "type": "Намуд",
            "language": "Забон",
            "students_count": "Шумораи хонандагон"
        }
        self.dynamic_widgets = {}
        self.dynamic_labels = []

        # SQLite Connection
        self.db = QSqlDatabase.addDatabase('QSQLITE')
        self.db.setDatabaseName(os.path.join(BASE_DIR, 'schools.db'))
        self.db.open()
        self.setup_database()
        # JSON helper functions
        self.analytics_db = DatabaseManager()


        self.central_widget = QWidget()
        self.setCentralWidget(self.central_widget)
        self.main_layout = QVBoxLayout(self.central_widget)

        # Header section (Global)
        self.header_layout = QHBoxLayout()
        self.header_layout.addStretch()
        
        self.add_image(self.header_layout, "assets/gerb.png", is_gif=False)
        
        self.title_label = QLabel("Шӯъбаи маорифи ноҳияи Зафаробод")
        self.title_label.setObjectName("titleLabel")
        self.title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title_font = QFont("Arial", 32)
        title_font.setWeight(QFont.Weight.Bold)
        self.title_label.setFont(title_font)
        self.header_layout.addWidget(self.title_label)

        self.add_image(self.header_layout, "assets/flag.gif", is_gif=True, width=180, height=120)
        self.header_layout.addStretch()
        
        self.main_layout.addLayout(self.header_layout, stretch=0)

        self.tabs = QTabWidget()
        self.main_layout.addWidget(self.tabs, stretch=1)

        self.tab1 = QWidget()
        self.tab2 = QWidget()
        self.tab3 = QWidget()
        self.tab4 = QWidget()

        self.tabs.addTab(self.tab1, "Ворид намудани маълумот ва таҳрир")
        self.tabs.addTab(self.tab2, "Омор ва инфографика")
        self.tabs.addTab(self.tab3, "Муаррифии автоматӣ")
        self.tabs.addTab(self.tab4, "Кормандон ва кадрҳо")

        # Tab 1 layout
        self.tab1_layout = QVBoxLayout(self.tab1)

        # Input fields Grid Layout
        self.input_layout = QGridLayout()
        self.input_name = QLineEdit()
        self.input_director = QLineEdit()
        self.input_phone = QLineEdit()
        
        self.dropdown_school_type = QComboBox()
        self.dropdown_school_type.addItems(["Мактаб", "Литсей", "Кӯдакистон", "Идораи маориф"])
        
        self.dropdown_language = QComboBox()
        self.dropdown_language.addItems(["Тоҷикӣ", "Ӯзбекӣ", "Русӣ"])
        
        self.input_students_count = QLineEdit()

        # Row 0
        self.input_layout.addWidget(QLabel("Номи мактаб:"), 0, 0)
        self.input_layout.addWidget(self.input_name, 0, 1)
        self.input_layout.addWidget(QLabel("Намуди муассаса:"), 0, 2)
        self.input_layout.addWidget(self.dropdown_school_type, 0, 3)
        self.input_layout.addWidget(QLabel("ФИО Директор:"), 0, 4)
        self.input_layout.addWidget(self.input_director, 0, 5)

        # Row 1
        self.input_layout.addWidget(QLabel("Забон:"), 1, 0)
        self.input_layout.addWidget(self.dropdown_language, 1, 1)
        self.input_layout.addWidget(QLabel("Рақами телефон:"), 1, 2)
        self.input_layout.addWidget(self.input_phone, 1, 3)
        self.input_layout.addWidget(QLabel("Шумораи хонандагон:"), 1, 4)
        self.input_layout.addWidget(self.input_students_count, 1, 5)

        self.tab1_layout.addLayout(self.input_layout)
        self.tab1_layout.addSpacing(15)

        # Buttons layout
        self.add_buttons_layout = QHBoxLayout()
        
        self.btn = QPushButton("Илова кардан")
        self.btn.clicked.connect(self.add_school)
        self.add_buttons_layout.addWidget(self.btn)
        
        self.btn_add_column = QPushButton("Иловаи устуни нав")
        self.btn_add_column.setObjectName("btnAddColumn")
        self.btn_add_column.clicked.connect(self.prompt_add_column)
        self.add_buttons_layout.addWidget(self.btn_add_column)
        
        self.btn_edit_column = QPushButton("Таҳрири устун")
        self.btn_edit_column.setObjectName("btnEditColumn")
        self.btn_edit_column.clicked.connect(self.prompt_edit_column)
        self.add_buttons_layout.addWidget(self.btn_edit_column)
        
        self.btn_delete_column = QPushButton("Ҳазфи устун")
        self.btn_delete_column.setObjectName("btnDeleteColumn")
        self.btn_delete_column.clicked.connect(self.prompt_delete_column)
        self.add_buttons_layout.addWidget(self.btn_delete_column)
        
        self.tab1_layout.addLayout(self.add_buttons_layout)

        # QTableView setup
        self.table = ExcelTableView()
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.horizontalHeader().setMinimumSectionSize(130)
        self.table.setSortingEnabled(True)
        self.table.setMaximumHeight(220)
        self.tab1_layout.addWidget(self.table)

        # Save and Delete Row Buttons
        self.table_actions_layout = QHBoxLayout()
        
        self.btn_save = QPushButton("Сабти тағйирот")
        self.btn_save.setObjectName("btnSave")
        self.btn_save.clicked.connect(self.save_changes)
        
        self.btn_delete = QPushButton("Ҳазф кардан")
        self.btn_delete.setObjectName("btnDelete")
        self.btn_delete.clicked.connect(self.delete_school)

        self.btn_export = QPushButton("Экспорт ба Excel")
        self.btn_export.setObjectName("btnExport")
        self.btn_export.clicked.connect(self.open_export_dialog)
        
        self.btn_import_json = QPushButton("📥 ИМПОРТИ МАЪЛУМОТҲОИ МАКТАБ (JSON)")
        self.btn_import_json.setObjectName("btnImportJson")
        self.btn_import_json.clicked.connect(self.open_import_json_dialog)
        
        self.table_actions_layout.addWidget(self.btn_save)
        self.table_actions_layout.addWidget(self.btn_delete)
        self.table_actions_layout.addWidget(self.btn_export)
        self.table_actions_layout.addWidget(self.btn_import_json)
        self.tab1_layout.addLayout(self.table_actions_layout)

        # Tab 2 layout
        self.tab2_layout = QHBoxLayout(self.tab2)
        self.tab2_layout.setContentsMargins(20, 20, 20, 20)
        self.tab2_layout.setSpacing(20)
        
        # Left Column: Analytical Statistics Dashboard
        self.tab2_left = QWidget()
        self.tab2_left_layout = QVBoxLayout(self.tab2_left)
        self.tab2_left_layout.setSpacing(15)
        
        # Compact General Stats Frame
        self.frame_general_stats = QFrame()
        self.frame_general_stats.setStyleSheet("""
            QFrame {
                background-color: white;
                border-radius: 12px;
                border: 1px solid #e9ecef;
            }
        """)
        shadow_stats = QGraphicsDropShadowEffect()
        shadow_stats.setBlurRadius(15)
        shadow_stats.setYOffset(3)
        shadow_stats.setColor(QColor(0, 0, 0, 15))
        self.frame_general_stats.setGraphicsEffect(shadow_stats)
        
        general_stats_layout = QGridLayout(self.frame_general_stats)
        general_stats_layout.setContentsMargins(10, 10, 10, 10)
        general_stats_layout.setSpacing(10)
        
        def add_stat_widget(row, col, icon, title, label):
            container = QFrame()
            container.setStyleSheet("background-color: #f8f9fa; border-radius: 8px; border: 1px solid #e9ecef;")
            vbox = QVBoxLayout(container)
            vbox.setContentsMargins(8, 6, 8, 6)
            vbox.setSpacing(2)
            header = QLabel(f"{icon} {title}")
            header.setStyleSheet("font-size: 11px; color: #666;")
            label.setStyleSheet("font-size: 20px; font-weight: bold; color: #003366;")
            vbox.addWidget(header)
            vbox.addWidget(label)
            general_stats_layout.addWidget(container, row, col)
        
        self.lbl_institutions_count = QLabel("0")
        self.lbl_students_count = QLabel("0")
        self.lbl_staff_count = QLabel("0")
        self.lbl_ratio = QLabel("0.0")
        
        add_stat_widget(0, 0, "🏢", "Муассисаҳо", self.lbl_institutions_count)
        add_stat_widget(0, 1, "👥", "Хонандагон", self.lbl_students_count)
        add_stat_widget(1, 0, "💼", "Кормандон", self.lbl_staff_count)
        add_stat_widget(1, 1, "📊", "Таносуб", self.lbl_ratio)
        
        self.tab2_left_layout.addWidget(self.frame_general_stats)
        
        # School Rankings Frame
        self.frame_school_rankings = QFrame()
        self.frame_school_rankings.setStyleSheet("""
            QFrame {
                background-color: white;
                border-radius: 10px;
                border: 1px solid #e0e0e0;
            }
        """)
        shadow_school = QGraphicsDropShadowEffect()
        shadow_school.setBlurRadius(15)
        shadow_school.setColor(QColor(0, 0, 0, 80))
        shadow_school.setOffset(0, 2)
        self.frame_school_rankings.setGraphicsEffect(shadow_school)
        
        school_rankings_layout = QVBoxLayout(self.frame_school_rankings)
        school_rankings_layout.setContentsMargins(12, 12, 12, 12)
        school_rankings_layout.setSpacing(10)
        
        school_rankings_title = QLabel("🏆 Рейтинги муассисаҳо:")
        school_rankings_title.setStyleSheet("font-size: 16px; font-weight: bold; color: #003366;")
        school_rankings_layout.addWidget(school_rankings_title)
        
        self.table_school_rankings = QTableWidget()
        self.table_school_rankings.setColumnCount(3)
        self.table_school_rankings.setHorizontalHeaderLabels(["№", "Муассиса", "Холи миёна"])
        self.table_school_rankings.setStyleSheet("""
            QTableWidget {
                border: none;
                background-color: white;
                gridline-color: #e9ecef;
            }
            QHeaderView::section {
                background-color: #003366;
                color: white;
                padding: 8px;
                font-weight: bold;
                border: none;
                border-bottom: 2px solid #003366;
            }
            QTableWidget::item {
                padding: 6px;
                border-bottom: 1px solid #e9ecef;
            }
            QTableWidget::item:selected {
                background-color: #e3f2fd;
                color: #003366;
            }
        """)
        self.table_school_rankings.horizontalHeader().setStretchLastSection(True)
        self.table_school_rankings.verticalHeader().setVisible(False)
        self.table_school_rankings.setMaximumHeight(250)
        school_rankings_layout.addWidget(self.table_school_rankings)
        
        self.tab2_left_layout.addWidget(self.frame_school_rankings)
        
        # Class Rankings Frame
        self.frame_class_rankings = QFrame()
        self.frame_class_rankings.setStyleSheet("""
            QFrame {
                background-color: white;
                border-radius: 10px;
                border: 1px solid #e0e0e0;
            }
        """)
        shadow_rankings = QGraphicsDropShadowEffect()
        shadow_rankings.setBlurRadius(15)
        shadow_rankings.setColor(QColor(0, 0, 0, 80))
        shadow_rankings.setOffset(0, 2)
        self.frame_class_rankings.setGraphicsEffect(shadow_rankings)
        
        rankings_layout = QVBoxLayout(self.frame_class_rankings)
        rankings_layout.setContentsMargins(12, 12, 12, 12)
        rankings_layout.setSpacing(10)
        
        # Ranking Controls
        rankings_header_layout = QHBoxLayout()
        
        rankings_title = QLabel("🏆 Рейтинги синфҳо:")
        rankings_title.setStyleSheet("font-size: 16px; font-weight: bold; color: #003366;")
        rankings_header_layout.addWidget(rankings_title)
        
        rankings_header_layout.addStretch()
        
        self.combo_rank_school_filter = QComboBox()
        self.combo_rank_school_filter.addItem("Ҳамаи мактабҳо")
        self.combo_rank_school_filter.setStyleSheet("padding: 5px; border: 1px solid #ccc; border-radius: 5px;")
        rankings_header_layout.addWidget(QLabel("Муассиса:"))
        rankings_header_layout.addWidget(self.combo_rank_school_filter)
        
        # Connect school filter to trigger ranking calculation
        self.combo_rank_school_filter.currentTextChanged.connect(self.calculate_class_rankings)
        
        rankings_layout.addLayout(rankings_header_layout)
        
        # Class Rankings Table
        self.table_class_rankings = QTableWidget()
        self.table_class_rankings.setColumnCount(6)
        self.table_class_rankings.setHorizontalHeaderLabels(["№", "Муассиса", "Синф", "Ҷойи мактабӣ", "Ҷойи ноҳиявӣ", "Холи миёна"])
        self.table_class_rankings.setStyleSheet("""
            QTableWidget {
                border: none;
                background-color: white;
                gridline-color: #e9ecef;
            }
            QHeaderView::section {
                background-color: #003366;
                color: white;
                padding: 8px;
                font-weight: bold;
                border: none;
                border-bottom: 2px solid #003366;
            }
            QTableWidget::item {
                padding: 6px;
                border-bottom: 1px solid #e9ecef;
            }
            QTableWidget::item:selected {
                background-color: #e3f2fd;
                color: #003366;
            }
        """)
        self.table_class_rankings.horizontalHeader().setStretchLastSection(True)
        self.table_class_rankings.verticalHeader().setVisible(False)
        rankings_layout.addWidget(self.table_class_rankings)
        
        self.tab2_left_layout.addStretch(1)
        
        # Right Column: Class Rankings Dashboard
        self.tab2_right = QWidget()
        self.tab2_right_layout = QVBoxLayout(self.tab2_right)
        self.tab2_right_layout.setSpacing(15)
        
        self.tab2_right_layout.addWidget(self.frame_class_rankings)
        
        # Subject Rankings Frame
        self.frame_subject_rankings = QFrame()
        self.frame_subject_rankings.setStyleSheet("""
            QFrame {
                background-color: white;
                border-radius: 10px;
                border: 1px solid #e0e0e0;
            }
        """)
        shadow_subjects = QGraphicsDropShadowEffect()
        shadow_subjects.setBlurRadius(15)
        shadow_subjects.setColor(QColor(0, 0, 0, 80))
        shadow_subjects.setOffset(0, 2)
        self.frame_subject_rankings.setGraphicsEffect(shadow_subjects)
        
        subject_rankings_layout = QVBoxLayout(self.frame_subject_rankings)
        subject_rankings_layout.setContentsMargins(12, 12, 12, 12)
        subject_rankings_layout.setSpacing(10)
        
        subject_rankings_title = QLabel("📊 Рейтинги фанҳо:")
        subject_rankings_title.setStyleSheet("font-size: 16px; font-weight: bold; color: #003366;")
        subject_rankings_layout.addWidget(subject_rankings_title)
        
        self.table_subject_rankings = QTableWidget()
        self.table_subject_rankings.setColumnCount(3)
        self.table_subject_rankings.setHorizontalHeaderLabels(["№", "Фан", "Холи миёна"])
        self.table_subject_rankings.setStyleSheet("""
            QTableWidget {
                border: none;
                background-color: white;
                gridline-color: #e9ecef;
            }
            QHeaderView::section {
                background-color: #003366;
                color: white;
                padding: 8px;
                font-weight: bold;
                border: none;
                border-bottom: 2px solid #003366;
            }
            QTableWidget::item {
                padding: 6px;
                border-bottom: 1px solid #e9ecef;
            }
            QTableWidget::item:selected {
                background-color: #e3f2fd;
                color: #003366;
            }
        """)
        self.table_subject_rankings.horizontalHeader().setStretchLastSection(True)
        self.table_subject_rankings.verticalHeader().setVisible(False)
        self.table_subject_rankings.setMaximumHeight(200)
        subject_rankings_layout.addWidget(self.table_subject_rankings)
        
        self.tab2_right_layout.addWidget(self.frame_subject_rankings)
        
        self.tab2_layout.addWidget(self.tab2_left, 1)
        self.tab2_layout.addWidget(self.tab2_right, 1)
        
        # Load statistics and contacts
        self.load_tab2_statistics()

        # Tab 3 layout
        self.tab3_layout = QVBoxLayout(self.tab3)
        self.setup_tab3_analytics()

        # Tab 4 layout
        self.tab4_layout = QHBoxLayout(self.tab4)
        
        # Left Panel (Form - 1/3)
        self.tab4_left = QWidget()
        self.tab4_left.setMaximumHeight(340)
        self.tab4_left_layout = QVBoxLayout(self.tab4_left)
        self.tab4_left_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        
        self.tab4_left_grid = QGridLayout()
        
        self.combo_select_school = QComboBox()
        self.combo_select_school.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.combo_select_school.currentIndexChanged.connect(self.filter_teachers_by_school)
        self.tab4_left_grid.addWidget(QLabel("Мактабро интихоб кунед:"), 0, 0)
        self.tab4_left_grid.addWidget(self.combo_select_school, 0, 1)
        
        self.input_teacher_name = QLineEdit()
        self.input_teacher_name.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.tab4_left_grid.addWidget(QLabel("ФИО Корманд:"), 0, 2)
        self.tab4_left_grid.addWidget(self.input_teacher_name, 0, 3)
        
        self.combo_subject = QComboBox()
        self.combo_subject.setEditable(True)
        self.combo_subject.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.tab4_left_grid.addWidget(QLabel("Вазифа ё Фан:"), 1, 0)
        self.tab4_left_grid.addWidget(self.combo_subject, 1, 1)
        
        self.input_teacher_age = QLineEdit()
        self.input_teacher_age.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.tab4_left_grid.addWidget(QLabel("Соли таваллуд:"), 1, 2)
        self.tab4_left_grid.addWidget(self.input_teacher_age, 1, 3)
        
        self.input_teacher_experience = QLineEdit()
        self.input_teacher_experience.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.tab4_left_grid.addWidget(QLabel("Соли собиқа (Стаж):"), 2, 0)
        self.tab4_left_grid.addWidget(self.input_teacher_experience, 2, 1)
        
        self.combo_teacher_category = QComboBox()
        self.combo_teacher_category.addItems(["Олӣ", "Якум", "Дуюм", "Бетоифа"])
        self.combo_teacher_category.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.tab4_left_grid.addWidget(QLabel("Тоифаи касбӣ:"), 2, 2)
        self.tab4_left_grid.addWidget(self.combo_teacher_category, 2, 3)
        
        self.input_teacher_phone = QLineEdit()
        self.input_teacher_phone.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.tab4_left_grid.addWidget(QLabel("Телефон:"), 3, 0)
        self.tab4_left_grid.addWidget(self.input_teacher_phone, 3, 1)

        self.chk_is_teacher = QCheckBox("Омӯзгор аст")
        self.chk_is_teacher.setChecked(True)
        self.tab4_left_grid.addWidget(self.chk_is_teacher, 3, 2, 1, 2)
        
        self.input_teacher_education = QLineEdit()
        self.input_teacher_education.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.tab4_left_grid.addWidget(QLabel("Таҳсилот:"), 4, 0)
        self.tab4_left_grid.addWidget(self.input_teacher_education, 4, 1)
        
        self.current_photo_path = ""
        
        self.btn_browse_photo = QPushButton("Интихоби расм")
        self.btn_browse_photo.clicked.connect(self.browse_photo)
        self.tab4_left_grid.addWidget(self.btn_browse_photo, 5, 2, 1, 2)
        
        # Enforce equal column stretching for symmetric layout
        self.tab4_left_grid.setColumnStretch(0, 1)  # Label column 1
        self.tab4_left_grid.setColumnStretch(1, 2)  # Input column 1
        self.tab4_left_grid.setColumnStretch(2, 1)  # Label column 2
        self.tab4_left_grid.setColumnStretch(3, 2)  # Input column 2
        self.tab4_left_grid.setColumnMinimumWidth(1, 150)
        self.tab4_left_grid.setColumnMinimumWidth(3, 150)
        
        self.tab4_left_layout.addLayout(self.tab4_left_grid)
        
        self.tab4_left_buttons_layout = QHBoxLayout()
        self.btn_add_teacher = QPushButton("Иловаи корманд")
        self.btn_add_teacher.setObjectName("btnAddTeacher")
        self.btn_add_teacher.clicked.connect(self.add_teacher)
        self.btn_delete_teacher = QPushButton("Ҳазфи корманд")
        self.btn_delete_teacher.setObjectName("btnDeleteTeacher")
        self.btn_delete_teacher.clicked.connect(self.delete_teacher)
        
        self.tab4_left_buttons_layout.addWidget(self.btn_add_teacher)
        self.tab4_left_buttons_layout.addWidget(self.btn_delete_teacher)
        self.tab4_left_layout.addLayout(self.tab4_left_buttons_layout)
        self.tab4_left_layout.addStretch(1)
        
        # Right Panel (Table & Profile - 2/3)
        self.tab4_right = QWidget()
        self.tab4_right_layout = QVBoxLayout(self.tab4_right)
        
        self.table_teachers = ExcelTableView()
        self.table_teachers.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.table_teachers.horizontalHeader().setStretchLastSection(True)
        self.table_teachers.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table_teachers.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.tab4_right_layout.addWidget(self.table_teachers, 1)
        
        self.profile_card_tab4 = QWidget()
        self.profile_card_tab4.setObjectName("profileCard")
        self.profile_card_tab4.setMaximumHeight(160)
        self.profile_layout_tab4 = QHBoxLayout(self.profile_card_tab4)
        self.profile_layout_tab4.setContentsMargins(6, 6, 6, 6)
        self.lbl_preview_photo = QLabel("Расм нест")
        self.lbl_preview_photo.setFixedSize(100, 130)
        self.lbl_preview_photo.setMaximumSize(100, 130)
        self.lbl_preview_photo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_preview_photo.setStyleSheet("border: 1px solid #ccc; background-color: white;")
        self.lbl_preview_info = QTextBrowser()
        self.lbl_preview_info.setHtml("<p style='font-size: 13px; color: #64748B;'>Маълумоти корманд дар ин ҷо пайдо мешавад...</p>")
        self.lbl_preview_info.setFrameShape(QFrame.Shape.NoFrame)
        self.lbl_preview_info.setStyleSheet("background: transparent;")
        self.lbl_preview_info.setMaximumHeight(140)
        self.profile_layout_tab4.addWidget(self.lbl_preview_photo)
        self.profile_layout_tab4.addWidget(self.lbl_preview_info, stretch=1)
        
        self.tab4_right_layout.addWidget(self.profile_card_tab4)
        self.tab4_right_layout.addStretch(1)
        
        self.tab4_layout.addWidget(self.tab4_left, stretch=1, alignment=Qt.AlignmentFlag.AlignTop)
        self.tab4_layout.addWidget(self.tab4_right, stretch=2)

        # Set up QSqlTableModel
        self.model = QSqlTableModel()
        self.model.setTable("schools")
        self.model.setEditStrategy(QSqlTableModel.EditStrategy.OnManualSubmit)
        self.table.setModel(self.model)
        
        self.teacher_model = QSqlTableModel()
        self.teacher_model.setTable("teachers")
        self.teacher_model.setEditStrategy(QSqlTableModel.EditStrategy.OnRowChange)
        self.table_teachers.setModel(self.teacher_model)
        self.setup_teacher_headers()
        self.teacher_model.dataChanged.connect(self.populate_subject_options)
        self.table_teachers.selectionModel().currentRowChanged.connect(self.display_teacher_profile)
        self.table_teachers.setColumnHidden(0, True)  # Hide teacher ID
        self.table_teachers.setColumnHidden(1, True)  # Hide school ID
        self.table_teachers.setColumnHidden(7, True)  # Hide photo path
        self.table_teachers.setColumnHidden(8, True)  # Hide is_teacher column
        
        self.setup_table_headers()
        self.refresh_dynamic_inputs()
        self.model.select()
        self.populate_school_dropdown()
        self.populate_subject_options()
        
        self.table.setColumnHidden(0, True)  # Hide ID
        self.table.sortByColumn(1, Qt.SortOrder.AscendingOrder)
        self.resize(1200, 800)
        self.rebuild_tab_order()
        
        # Refresh Tab 2 statistics on startup
        self.refresh_tab2_data()

    def add_image(self, layout, path, is_gif=False, width=120, height=120):
        label = QLabel()
        if os.path.exists(path):
            if is_gif:
                movie = QMovie(path)
                movie.setScaledSize(QSize(width, height))
                label.setMovie(movie)
                movie.start()
            else:
                pixmap = QPixmap(path)
                label.setPixmap(pixmap.scaled(width, height, Qt.AspectRatioMode.KeepAspectRatio))
        else:
            label.setText("Расм нест")
        layout.addWidget(label)


    def setup_database(self):
        query = QSqlQuery()
        # Create core tables
        query.exec("CREATE TABLE IF NOT EXISTS schools (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, director TEXT, phone TEXT, type TEXT)")
        query.exec("CREATE TABLE IF NOT EXISTS column_configs (column_name TEXT PRIMARY KEY, field_type TEXT, options TEXT)")
        query.exec("CREATE TABLE IF NOT EXISTS teachers (id INTEGER PRIMARY KEY AUTOINCREMENT, school_id INTEGER, name TEXT, subject TEXT, experience INTEGER, category TEXT, age INTEGER, photo_path TEXT, phone TEXT, education TEXT)")
        
        # Auto-clean duplicate schools on startup
        query.exec("DELETE FROM schools WHERE id NOT IN (SELECT MIN(id) FROM schools GROUP BY name)")
        
        # Auto-clean orphaned teachers on startup
        query.exec("DELETE FROM teachers WHERE school_id NOT IN (SELECT id FROM schools)")
        
        # Auto-clean duplicate teachers on startup (ЯНГИ ҚЎШИЛГАН ҚАТОР)
        query.exec("DELETE FROM teachers WHERE id NOT IN (SELECT MIN(id) FROM teachers GROUP BY name)")
        
        # Enforce missing columns safely on existing database
        query.exec("ALTER TABLE schools ADD COLUMN language TEXT")
        query.exec("ALTER TABLE schools ADD COLUMN students_count INTEGER")
        query.exec("ALTER TABLE teachers ADD COLUMN is_teacher INTEGER DEFAULT 1")
        query.exec("ALTER TABLE teachers ADD COLUMN phone TEXT")
        query.exec("ALTER TABLE teachers ADD COLUMN education TEXT")

    def get_custom_columns(self):
        columns = []
        query = QSqlQuery("PRAGMA table_info(schools)")
        while query.next():
            col_name = query.value(1)
            if col_name not in ['id', 'name', 'director', 'phone', 'type', 'language', 'students_count']:
                columns.append(col_name)
        return columns

    def setup_table_headers(self):
        query = QSqlQuery("PRAGMA table_info(schools)")
        idx = 0
        while query.next():
            col_name = query.value(1)
            header_text = self.default_headers.get(col_name, col_name)
            self.model.setHeaderData(idx, Qt.Orientation.Horizontal, header_text)
            idx += 1

    def populate_subject_options(self):
        current_text = self.combo_subject.currentText()
        self.combo_subject.blockSignals(True)
        self.combo_subject.clear()
        
        default_items = ["Директор", "Муовини директор", "Завхоз", "Қаровул", "Рӯбанда", "Ошпаз", "Котиба", "Математика", "Физика", "Химия", "Биология", "Забони тоҷикӣ", "Забони ӯзбекӣ", "Забони русӣ", "Забони англисӣ", "Таърих", "Технология", "Тарбияи ҷисмонӣ"]
        self.combo_subject.addItems(default_items)
        
        query = QSqlQuery("SELECT DISTINCT subject FROM teachers")
        while query.next():
            subject = query.value(0)
            if subject and subject not in default_items:
                self.combo_subject.addItem(subject)
                
        if current_text:
            self.combo_subject.setCurrentText(current_text)
        self.combo_subject.blockSignals(False)

    def setup_teacher_headers(self):
        self.teacher_model.setHeaderData(2, Qt.Orientation.Horizontal, "ФИО")
        self.teacher_model.setHeaderData(3, Qt.Orientation.Horizontal, "Вазифа ё Фан")
        self.teacher_model.setHeaderData(4, Qt.Orientation.Horizontal, "Собиқаи корӣ")
        self.teacher_model.setHeaderData(5, Qt.Orientation.Horizontal, "Тоифа")
        self.teacher_model.setHeaderData(6, Qt.Orientation.Horizontal, "Соли таваллуд")
        self.teacher_model.setHeaderData(9, Qt.Orientation.Horizontal, "Телефон")
        self.teacher_model.setHeaderData(10, Qt.Orientation.Horizontal, "Таҳсилот")

    def refresh_dynamic_inputs(self):
        # Clear existing dynamic widgets safely
        for widget in self.dynamic_widgets.values():
            self.input_layout.removeWidget(widget)
            widget.deleteLater()
        self.dynamic_widgets.clear()

        for label in self.dynamic_labels:
            self.input_layout.removeWidget(label)
            label.deleteLater()
        self.dynamic_labels.clear()

        # Query metadata
        configs = {}
        query = QSqlQuery("SELECT column_name, field_type, options FROM column_configs")
        while query.next():
            configs[query.value(0)] = (query.value(1), query.value(2))

        # Re-render inputs for custom columns
        custom_cols = self.get_custom_columns()
        row, col = 2, 0
        for col_name in custom_cols:
            label = QLabel(f"{col_name}:")
            field_type, options_str = configs.get(col_name, ("Матни оддӣ", ""))

            if field_type == "Рӯйхати интихобӣ (Dropdown)":
                widget = QComboBox()
                options = [opt.strip() for opt in options_str.split(",") if opt.strip()]
                widget.addItems(options)
            elif field_type == "Ҳа / Не":
                widget = QComboBox()
                widget.addItems(["Интихоб кунед", "Ҳа", "Не"])
            else:
                widget = QLineEdit()

            self.input_layout.addWidget(label, row, col)
            self.input_layout.addWidget(widget, row, col + 1)
            self.dynamic_widgets[col_name] = widget
            self.dynamic_labels.append(label)

            col += 2
            if col >= 6:
                col = 0
                row += 1

        self.rebuild_tab_order()

    def rebuild_tab_order(self):
        # Core default input widgets in logical order
        focus_chain = [
            self.input_name,
            self.dropdown_school_type,
            self.input_director,
            self.dropdown_language,
            self.input_phone,
            self.input_students_count
        ]

        # Add dynamic inputs sequentially in the order they were inserted
        for widget in self.dynamic_widgets.values():
            focus_chain.append(widget)

        # Add action buttons and tables at the end of the focus chain
        focus_chain.extend([
            self.btn,
            self.btn_add_column,
            self.btn_edit_column,
            self.btn_delete_column,
            self.table,
            self.btn_save,
            self.btn_delete,
            self.btn_export
        ])

        # Apply the tab order sequentially in PyQt6
        for i in range(len(focus_chain) - 1):
            self.setTabOrder(focus_chain[i], focus_chain[i + 1])

    def prompt_add_column(self):
        # Step 1: Get name
        col_name, ok1 = QInputDialog.getText(
            self, 
            "Устуни нав", 
            "Номи устуни навро ворид кунед (масалан: Намуди компютер):"
        )
        if not ok1 or not col_name.strip():
            return
        col_name = col_name.strip()

        # Step 2: Get Type
        types = ["Матни оддӣ", "Рӯйхати интихобӣ (Dropdown)", "Ҳа / Не"]
        col_type, ok2 = QInputDialog.getItem(
            self, 
            "Намуди устун", 
            "Намуди маълумоти устуни навро интихоб кунед:", 
            types, 
            0, 
            False
        )
        if not ok2:
            return

        # Step 3: Get options if Dropdown
        options = ""
        if col_type == "Рӯйхати интихобӣ (Dropdown)":
            options, ok3 = QInputDialog.getText(
                self, 
                "Унсурҳои рӯйхат", 
                "Унсурҳои рӯйхатро бо вергул ажратилган ҳолда ворид кунед (масалан: Pentium 1, Pentium 2, Core i3):"
            )
            if not ok3:
                return
            options = options.strip()

        # Update database schema
        query = QSqlQuery()
        if query.exec(f'ALTER TABLE schools ADD COLUMN "{col_name}" TEXT'):
            # Insert metadata config
            query.prepare("INSERT OR REPLACE INTO column_configs (column_name, field_type, options) VALUES (?, ?, ?)")
            query.addBindValue(col_name)
            query.addBindValue(col_type)
            query.addBindValue(options)
            query.exec()

            # Refresh table model schema & UI
            self.model.setTable("schools")
            self.setup_table_headers()
            self.refresh_dynamic_inputs()
            self.model.select()
            self.table.setColumnHidden(0, True)
            QMessageBox.information(self, "Муваффақият", f"Устуни нав '{col_name}' бо муваффақият илова шуд!")
        else:
            QMessageBox.warning(self, "Хатогӣ", f"Хатогӣ ҳангоми иловаи устун: {query.lastError().text()}")

    def prompt_delete_column(self):
        custom_cols = self.get_custom_columns()
        if not custom_cols:
            QMessageBox.warning(self, "Диққат", "Ягон устуни фармоишӣ барои ҳазф вуҷуд надорад.")
            return

        col_name, ok = QInputDialog.getItem(
            self, 
            "Ҳазфи устун", 
            "Устунро барои ҳазф интихоб кунед:", 
            custom_cols, 
            0, 
            False
        )
        if not ok:
            return

        msg = QMessageBox(self)
        msg.setIcon(QMessageBox.Icon.Warning)
        msg.setWindowTitle("Тасдиқ")
        msg.setText(f"Оё шумо дар ҳақиқат мехоҳед устуни '{col_name}'-ро ҳазф кунед? Ҳамаи маълумоти ин устун ҳазф хоҳад шуд!")
        yes_button = msg.addButton("Ҳа", QMessageBox.ButtonRole.YesRole)
        no_button = msg.addButton("Не", QMessageBox.ButtonRole.NoRole)
        msg.exec()

        if msg.clickedButton() == yes_button:
            # Safely clear the model to unlock active handles on SQLite
            self.model.clear()

            query = QSqlQuery()
            if query.exec(f'ALTER TABLE schools DROP COLUMN "{col_name}"'):
                # Delete configuration metadata
                query.prepare("DELETE FROM column_configs WHERE column_name = ?")
                query.addBindValue(col_name)
                query.exec()

                # Re-setup
                self.model.setTable("schools")
                self.setup_table_headers()
                self.model.select()
                self.refresh_dynamic_inputs()
                self.table.setColumnHidden(0, True)
                QMessageBox.information(self, "Муваффақият", "Устун бо муваффақият ҳазф карда шуд!")
            else:
                self.model.setTable("schools")
                self.setup_table_headers()
                self.model.select()
                self.table.setColumnHidden(0, True)
                QMessageBox.warning(self, "Хатогӣ", f"Хатогӣ ҳангоми ҳазф: {query.lastError().text()}")

    def prompt_edit_column(self):
        custom_cols = self.get_custom_columns()
        if not custom_cols:
            QMessageBox.warning(self, "Диққат", "Ягон устуни фармоишӣ барои таҳрир вуҷуд надорад.")
            return

        old_name, ok1 = QInputDialog.getItem(
            self, 
            "Таҳрири устун", 
            "Устунро барои тағйири ном интихоб кунед:", 
            custom_cols, 
            0, 
            False
        )
        if not ok1:
            return

        new_name, ok2 = QInputDialog.getText(
            self, 
            "Номи нав", 
            f"Номи нави устуни '{old_name}'-ро ворид кунед:"
        )
        if not ok2 or not new_name.strip():
            return
        new_name = new_name.strip()

        self.model.clear()
        query = QSqlQuery()
        if query.exec(f'ALTER TABLE schools RENAME COLUMN "{old_name}" TO "{new_name}"'):
            # Update configuration record
            query.prepare("UPDATE column_configs SET column_name = ? WHERE column_name = ?")
            query.addBindValue(new_name)
            query.addBindValue(old_name)
            query.exec()

            self.model.setTable("schools")
            self.setup_table_headers()
            self.model.select()
            self.refresh_dynamic_inputs()
            self.table.setColumnHidden(0, True)
            QMessageBox.information(self, "Муваффақият", "Номи устун бо муваффақият тағйир ёфт!")
        else:
            self.model.setTable("schools")
            self.setup_table_headers()
            self.model.select()
            self.table.setColumnHidden(0, True)
            QMessageBox.warning(self, "Хатогӣ", f"Хатогӣ ҳангоми тағйири ном: {query.lastError().text()}")

    def add_school(self):
        name = self.input_name.text().strip()
        if not name:
            QMessageBox.warning(self, "Хатогӣ", "Лутфан, номи мактабро ворид кунед!")
            return

        # Fetch actual columns except 'id'
        cols = []
        query = QSqlQuery("PRAGMA table_info(schools)")
        while query.next():
            col_name = query.value(1)
            if col_name != 'id':
                cols.append(col_name)

        # Build dynamic query
        col_str = ", ".join([f'"{c}"' for c in cols])
        val_str = ", ".join(["?" for _ in cols])
        sql = f"INSERT INTO schools ({col_str}) VALUES ({val_str})"

        query_insert = QSqlQuery()
        query_insert.prepare(sql)

        for c in cols:
            if c == "name":
                query_insert.addBindValue(name)
            elif c == "director":
                query_insert.addBindValue(self.input_director.text().strip())
            elif c == "phone":
                query_insert.addBindValue(self.input_phone.text().strip())
            elif c == "type":
                query_insert.addBindValue(self.dropdown_school_type.currentText())
            elif c == "language":
                query_insert.addBindValue(self.dropdown_language.currentText())
            elif c == "students_count":
                query_insert.addBindValue(self.input_students_count.text().strip())
            else:
                widget = self.dynamic_widgets.get(c)
                if widget:
                    if isinstance(widget, QComboBox):
                        val = widget.currentText()
                        if val == "Интихоб кунед":
                            val = ""
                    else:
                        val = widget.text().strip()
                    query_insert.addBindValue(val)
                else:
                    query_insert.addBindValue("")

        if query_insert.exec():
            self.model.select()
            self.populate_school_dropdown()
            self.input_name.clear()
            self.input_director.clear()
            self.input_phone.clear()
            self.input_students_count.clear()
            for w in self.dynamic_widgets.values():
                if isinstance(w, QComboBox):
                    w.setCurrentIndex(0)
                else:
                    w.clear()
            QMessageBox.information(self, "Муваффақият", "Маълумот бо муваффақият илова шуд!")
        else:
            QMessageBox.warning(self, "Хатогӣ", f"Хатогӣ ҳангоми иловаи маълумот: {query_insert.lastError().text()}")

    def save_changes(self):
        if self.model.submitAll():
            self.populate_school_dropdown()
            self.refresh_tab2_data()
            QMessageBox.information(self, "Муваффақият", "Тағйирот бо муваффақият сабт шуд!")
        else:
            QMessageBox.warning(self, "Хатогӣ", f"Хатогӣ ҳангоми сабти тағйирот: {self.model.lastError().text()}")

    def delete_school(self):
        selected_index = self.table.currentIndex()
        if not selected_index.isValid():
            QMessageBox.warning(self, "Диққат", "Лутфан, сатреро, ки мехоҳед ҳазф кунед, интихоб намоед!")
            return

        msg = QMessageBox(self)
        msg.setIcon(QMessageBox.Icon.Question)
        msg.setWindowTitle("Тасдиқ")
        msg.setText("Оё шумо дар ҳақиқат мехоҳед сатри интихобшударо ҳазф кунед?")
        yes_button = msg.addButton("Ҳа", QMessageBox.ButtonRole.YesRole)
        no_button = msg.addButton("Не", QMessageBox.ButtonRole.NoRole)
        msg.exec()

        if msg.clickedButton() == yes_button:
            self.model.removeRow(selected_index.row())
            if self.model.submitAll():
                QMessageBox.information(self, "Муваффақият", "Маълумот ҳазф карда шуд!")
                self.model.select()
                self.populate_school_dropdown()
                self.refresh_tab2_data()
            else:
                self.model.revertAll()
                QMessageBox.warning(self, "Хатогӣ", f"Хатогӣ ҳангоми ҳазф: {self.model.lastError().text()}")

    def open_export_dialog(self):
        # Fetch all columns dynamically
        cols = []
        query = QSqlQuery("PRAGMA table_info(schools)")
        while query.next():
            cols.append(query.value(1))

        dialog = ExportDialog(cols, self.default_headers, self)
        if dialog.exec():
            selected_cols = dialog.get_selected_columns()
            if not selected_cols:
                QMessageBox.warning(self, "Диққат", "Лутфан, ақаллан як устунро интихоб кунед!")
                return

            # Open File Save Dialog
            file_path, _ = QFileDialog.getSaveFileName(self, "Сабти ҳисобот", "", "Excel Files (*.csv)")
            if file_path:
                self.export_to_csv(file_path, selected_cols)

    def export_to_csv(self, file_path, selected_cols):
        import csv
        try:
            # Fetch data
            col_str = ", ".join([f'"{c}"' for c in selected_cols])
            query = QSqlQuery(f"SELECT {col_str} FROM schools")
            
            with open(file_path, mode='w', encoding='utf-8-sig', newline='') as file:
                writer = csv.writer(file, delimiter=';')
                
                # Write header row (translate headers using default_headers if mapped)
                headers = [self.default_headers.get(c, c) for c in selected_cols]
                writer.writerow(headers)
                
                # Write data rows
                while query.next():
                    row_data = []
                    for i in range(len(selected_cols)):
                        val = query.value(i)
                        if selected_cols[i] == "phone":
                            val = f'="{val}"'
                        row_data.append(val)
                    writer.writerow(row_data)
                    
            QMessageBox.information(self, "Муваффақият", "Ҳисобот бо муваффақият ба Excel экспорт карда шуд!")
        except Exception as e:
            QMessageBox.warning(self, "Хатогӣ", f"Хатогӣ ҳангоми экспорт: {str(e)}")

    def open_import_json_dialog(self):
        file_path, _ = QFileDialog.getOpenFileName(self, "Интихоби файли JSON", "", "JSON Files (*.json)")
        if file_path:
            success, msg = self.import_school_data_from_json(file_path)
            if success:
                QMessageBox.information(self, "Муваффақият", msg or "Маълумотҳои мактаб бомуваффақият ворид карда шуданд!")
                self.model.select()
                self.populate_school_dropdown()
            else:
                QMessageBox.warning(self, "Хатогӣ", f"Хатогӣ ҳангоми воридкунӣ: {msg}")

    def import_school_data_from_json(self, json_filepath, import_staff=True, import_grades=True):
        try:
            with open(json_filepath, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            school_name = data.get("school_name", "Номаълум")
            
            # Create/Find school to get school_id
            query = QSqlQuery()
            query.prepare("SELECT id FROM schools WHERE name = ?")
            query.addBindValue(school_name)
            if query.exec() and query.next():
                school_id = query.value(0)
            else:
                query.prepare("INSERT INTO schools (name) VALUES (?)")
                query.addBindValue(school_name)
                if query.exec():
                    school_id = query.lastInsertId()
                else:
                    school_id = -1

            if import_staff and "staff" in data:
                for emp in data["staff"]:
                    name = emp.get("Name", "")
                    position = emp.get("position", "")
                    exp = emp.get("experience", 0)
                    cat = emp.get("category", "")
                    phone = emp.get("phone", "")
                    edu = emp.get("education", "")
                    photo = emp.get("photo_path", "")
                    
                    query.prepare("SELECT id FROM teachers WHERE name = ? AND school_id = ?")
                    query.addBindValue(name)
                    query.addBindValue(school_id)
                    if query.exec() and query.next():
                        t_id = query.value(0)
                        query.prepare("""UPDATE teachers SET 
                            subject = ?, experience = ?, category = ?, 
                            phone = ?, education = ?, photo_path = ?
                            WHERE id = ?""")
                        query.addBindValue(position)
                        query.addBindValue(exp)
                        query.addBindValue(cat)
                        query.addBindValue(phone)
                        query.addBindValue(edu)
                        query.addBindValue(photo)
                        query.addBindValue(t_id)
                        query.exec()
                    else:
                        query.prepare("""INSERT INTO teachers 
                            (school_id, name, subject, experience, category, phone, education, photo_path)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""")
                        query.addBindValue(school_id)
                        query.addBindValue(name)
                        query.addBindValue(position)
                        query.addBindValue(exp)
                        query.addBindValue(cat)
                        query.addBindValue(phone)
                        query.addBindValue(edu)
                        query.addBindValue(photo)
                        query.exec()

            if import_grades and "students_grades" in data:
                cursor = self.analytics_db.conn.cursor()
                for sg in data["students_grades"]:
                    student_name = sg.get("student_name", "")
                    class_name = sg.get("class_name", "")
                    student_id = f"{school_name}__{class_name}__{student_name}"
                    
                    cursor.execute("INSERT OR IGNORE INTO students (id, full_name, class_name, school_name) VALUES (?, ?, ?, ?)", 
                                   (student_id, student_name, class_name, school_name))
                    
                    grades = sg.get("grades", {})
                    for period, score in grades.items():
                        cursor.execute("DELETE FROM grades WHERE student_id = ? AND subject = ?", (student_id, period))
                        cursor.execute("INSERT INTO grades (student_id, subject, score) VALUES (?, ?, ?)", (student_id, period, float(score)))
                
                self.analytics_db.conn.commit()

            # Refresh Tab 2 statistics after import
            self.refresh_tab2_data()

            return True, "Маълумотҳои мактаб бомуваффақият ворид карда шуданд!"
        except Exception as e:
            return False, f"Хатогии импорт: {str(e)}"

    def populate_school_dropdown(self):
        self.combo_select_school.clear()
        query = QSqlQuery("SELECT id, name FROM schools ORDER BY name ASC")
        while query.next():
            self.combo_select_school.addItem(query.value(1), query.value(0))
        if self.combo_select_school.count() > 0:
            self.filter_teachers_by_school()

    def filter_teachers_by_school(self):
        school_id = self.combo_select_school.currentData()
        if school_id is not None:
            self.teacher_model.setFilter(f"school_id = {school_id}")
            self.teacher_model.select()
            self.lbl_preview_photo.setText("Расм нест")
            self.lbl_preview_photo.setPixmap(QPixmap())
            self.lbl_preview_info.setText("Маълумоти корманд дар ин ҷо пайдо мешавад...")

    def browse_photo(self):
        file_path, _ = QFileDialog.getOpenFileName(self, "Интихоби расм", "", "Images (*.png *.jpg *.jpeg)")
        if file_path:
            self.current_photo_path = file_path
            
            selected_index = self.table_teachers.currentIndex()
            if selected_index.isValid():
                row = selected_index.row()
                teacher_id = self.teacher_model.index(row, 0).data()
                if teacher_id is not None:
                    query = QSqlQuery()
                    query.prepare("UPDATE teachers SET photo_path = ? WHERE id = ?")
                    query.addBindValue(file_path)
                    query.addBindValue(teacher_id)
                    if query.exec():
                        self.teacher_model.select()
                        self.display_teacher_profile(self.table_teachers.currentIndex(), None)

    def add_teacher(self):
        school_id = self.combo_select_school.currentData()
        if school_id is None:
            QMessageBox.warning(self, "Диққат", "Лутфан мактабро интихоб кунед!")
            return
            
        name = self.input_teacher_name.text().strip()
        if not name:
            QMessageBox.warning(self, "Хатогӣ", "Лутфан, ФИО муаллимро ворид кунед!")
            return

        subject = self.combo_subject.currentText()
        age = self.input_teacher_age.text().strip()
        experience = self.input_teacher_experience.text().strip()
        category = self.combo_teacher_category.currentText()
        is_teacher = 1 if self.chk_is_teacher.isChecked() else 0
        photo_path = self.current_photo_path
        phone = self.input_teacher_phone.text().strip()
        education = self.input_teacher_education.text().strip()

        query = QSqlQuery()
        query.prepare("INSERT INTO teachers (school_id, name, subject, experience, category, age, photo_path, is_teacher, phone, education) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)")
        query.addBindValue(school_id)
        query.addBindValue(name)
        query.addBindValue(subject)
        query.addBindValue(experience)
        query.addBindValue(category)
        query.addBindValue(age)
        query.addBindValue(photo_path)
        query.addBindValue(is_teacher)
        query.addBindValue(phone)
        query.addBindValue(education)

        if query.exec():
            self.teacher_model.select()
            self.populate_subject_options()
            self.refresh_tab2_data()
            self.input_teacher_name.clear()
            self.input_teacher_age.clear()
            self.input_teacher_experience.clear()
            self.input_teacher_phone.clear()
            self.input_teacher_education.clear()
            self.current_photo_path = ""
            self.chk_is_teacher.setChecked(True)
            QMessageBox.information(self, "Муваффақият", "Муаллим бо муваффақият илова шуд!")
        else:
            QMessageBox.warning(self, "Хатогӣ", f"Хатогӣ ҳангоми иловаи маълумот: {query.lastError().text()}")

    def delete_teacher(self):
        selected_index = self.table_teachers.currentIndex()
        if not selected_index.isValid():
            QMessageBox.warning(self, "Диққат", "Лутфан, сатреро, ки мехоҳед ҳазф кунед, интихоб намоед!")
            return

        msg = QMessageBox(self)
        msg.setIcon(QMessageBox.Icon.Question)
        msg.setWindowTitle("Тасдиқ")
        msg.setText("Оё шумо дар ҳақиқат мехоҳед ин муаллимро ҳазф кунед?")
        yes_button = msg.addButton("Ҳа", QMessageBox.ButtonRole.YesRole)
        no_button = msg.addButton("Не", QMessageBox.ButtonRole.NoRole)
        msg.exec()

        if msg.clickedButton() == yes_button:
            self.teacher_model.removeRow(selected_index.row())
            if self.teacher_model.submitAll():
                QMessageBox.information(self, "Муваффақият", "Маълумот ҳазф карда шуд!")
                self.teacher_model.select()
                self.populate_subject_options()
                self.refresh_tab2_data()
            else:
                self.teacher_model.revertAll()
                QMessageBox.warning(self, "Хатогӣ", f"Хатогӣ ҳангоми ҳазф: {self.teacher_model.lastError().text()}")

    def display_teacher_profile(self, current, previous):
        if not current.isValid():
            self.lbl_preview_photo.setText("Расм нест")
            self.lbl_preview_photo.setPixmap(QPixmap())
            self.lbl_preview_info.setHtml("<p style='font-size: 13px; color: #64748B;'>Маълумоти корманд дар ин ҷо пайдо мешавад...</p>")
            return

        row = current.row()
        name = self.teacher_model.index(row, 2).data()
        subject = self.teacher_model.index(row, 3).data()
        experience = self.teacher_model.index(row, 4).data()
        category = self.teacher_model.index(row, 5).data()
        age = self.teacher_model.index(row, 6).data()
        photo_path = self.teacher_model.index(row, 7).data()
        is_teacher = self.teacher_model.index(row, 8).data()
        phone_data = self.teacher_model.index(row, 9).data()
        phone = phone_data if phone_data else "Маълумот нест"
        education_data = self.teacher_model.index(row, 10).data()
        education = education_data if education_data else "Маълумот нест"

        if is_teacher in (1, "1", True):
            role_display = f"Омӯзгори фанни {subject}"
        else:
            role_display = f"Вазифа: {subject}"

        info_html = f"""
        <div style='padding: 0px; line-height: 1.1; font-family: Arial, sans-serif;'>
            <h2 style='color: #003366; font-size: 18px; margin: 1px 0 3px 0;'><b>👤 {name}</b></h2>
            <p style='font-size: 12px; color: #333; margin: 1px 0; line-height: 1.1;'>💼 <b>Лавозим:</b> <span style='color: #003366; font-weight: bold;'>{role_display}</span></p>
            <p style='font-size: 12px; color: #333; margin: 1px 0; line-height: 1.1;'>🎓 <b>Таҳсилот:</b> <span style='color: #003366; font-weight: bold;'>{education}</span></p>
            <p style='font-size: 12px; color: #333; margin: 1px 0; line-height: 1.1;'>📅 <b>Соли таваллуд:</b> <span style='color: #003366; font-weight: bold;'>{age} сол</span></p>
            <p style='font-size: 12px; color: #333; margin: 1px 0; line-height: 1.1;'>⏱️ <b>Собиқаи корӣ (Стаж):</b> <span style='color: #003366; font-weight: bold;'>{experience} сол</span></p>
            <p style='font-size: 12px; color: #333; margin: 1px 0; line-height: 1.1;'>🏅 <b>Тоифаи касбӣ:</b> <span style='color: white; background-color: #28a745; padding: 1px 5px; border-radius: 3px; font-weight: bold;'>{category}</span></p>
            <p style='font-size: 12px; color: #333; margin: 1px 0; line-height: 1.1;'>📞 <b>Телефон:</b> <span style='color: #003366; font-weight: bold;'>{phone}</span></p>
        </div>
        """
        self.lbl_preview_info.setHtml(info_html)
        
        if photo_path and os.path.exists(photo_path):
            pixmap = QPixmap(photo_path)
            self.lbl_preview_photo.setPixmap(pixmap.scaled(120, 140, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
        else:
            self.lbl_preview_photo.setPixmap(QPixmap())
            self.lbl_preview_photo.setText("Расм нест")
            
        self.lbl_preview_photo.setAlignment(Qt.AlignmentFlag.AlignCenter)

    def get_stylesheet(self):
        return """
            QMainWindow { background-color: #f8f9fa; } 
            #profileCard { background-color: #f1f3f5; border-radius: 4px; padding: 10px; }
            #btnAddTeacher { background-color: #28a745; color: white; padding: 8px 12px; font-weight: bold; border-radius: 4px; }
            #btnAddTeacher:hover { background-color: #218838; }
            #btnDeleteTeacher { background-color: #dc3545; color: white; padding: 8px 12px; font-weight: bold; border-radius: 4px; }
            #btnDeleteTeacher:hover { background-color: #c82333; }
            #titleLabel { font-size: 32px; font-weight: bold; color: #003366; margin: 0 20px; }
            QLabel { font-size: 13px; font-weight: bold; color: #003366; }
            QLineEdit, QComboBox { font-size: 13px; padding: 6px; border: 1px solid #b0c4de; border-radius: 4px; background-color: white; }
            QLineEdit:focus, QComboBox:focus { border: 1px solid #003366; }
            
            QPushButton { background-color: #003366; color: white; padding: 8px 12px; font-size: 13px; font-weight: bold; border-radius: 4px; }
            QPushButton:hover { background-color: #004080; }
            
            #btnAddColumn { background-color: #17a2b8; }
            #btnAddColumn:hover { background-color: #138496; }
            #btnEditColumn { background-color: #6c757d; }
            #btnEditColumn:hover { background-color: #5a6268; }
            #btnDeleteColumn { background-color: #5a6268; }
            #btnDeleteColumn:hover { background-color: #495057; }
            
            #btnSave { background-color: #28a745; color: white; padding: 8px 12px; }
            #btnSave:hover { background-color: #218838; }
            #btnDelete { background-color: #dc3545; color: white; padding: 8px 12px; }
            #btnDelete:hover { background-color: #c82333; }
            
            #btnExport, #btnExecuteExport { background-color: #e67e22; color: white; padding: 8px 12px; font-size: 13px; font-weight: bold; border-radius: 4px; }
            #btnExport:hover, #btnExecuteExport:hover { background-color: #d35400; }
            
            QTableView { background-color: white; gridline-color: #dcdcdc; border: 1px solid #b0c4de; font-size: 12px; }
            QTableView QLineEdit { 
                padding: 4px; 
                border: 1px solid #003366; 
                background-color: #f8f9fa; 
                font-size: 12px; 
                font-weight: bold;
            }
            QHeaderView::section { background-color: #e9ecef; color: #003366; font-weight: bold; padding: 5px; border: 1px solid #dcdcdc; }
            
            QTabWidget::pane { border: 1px solid #b0c4de; background-color: white; border-radius: 4px; }
            QTabBar::tab { background-color: #e9ecef; color: #003366; padding: 10px 20px; font-size: 14px; font-weight: bold; border: 1px solid #b0c4de; border-bottom: none; border-top-left-radius: 4px; border-top-right-radius: 4px; margin-right: 2px; }
            QTabBar::tab:selected { background-color: white; color: #003366; border-bottom: 2px solid white; }
            QTabBar::tab:hover { background-color: #d0d5db; }
        """


    # ==========================================
    # JSON EXPORT/IMPORT HELPERS
    # ==========================================
    def export_school_data_to_json(self, school_id):
        # Placeholder for exporting school data to JSON
        print(f"Exporting data for school_id: {school_id} to JSON (Not implemented yet)")
        pass

    # ==========================================
    # ANALYTICS METHODS FOR TAB 3
    # ==========================================
    def setup_tab3_analytics(self):
        self.tab3_layout.setContentsMargins(0, 0, 0, 0)
        self.tab3_layout.setSpacing(0)

        # ------------------------------------------------------
        # ПАНЕЛИ ЧАП (SIDEBAR)
        # ------------------------------------------------------
        self.sidebar = QFrame()
        self.sidebar.setMinimumWidth(300)
        self.sidebar.setMaximumWidth(400)
        self.sidebar.setStyleSheet("""
            QFrame { background-color: #0F172A; border-right: 1px solid #1E293B; }
        """)
        self.sidebar_layout = QVBoxLayout(self.sidebar)
        self.sidebar_layout.setContentsMargins(15, 15, 15, 15)
        self.sidebar_layout.setSpacing(8)

        # Compact grid layout for sidebar controls
        self.sidebar_controls_grid = QGridLayout()
        self.sidebar_controls_grid.setSpacing(6)
        self.sidebar_controls_grid.setContentsMargins(0, 0, 0, 0)

        lbl_school = QLabel("Мактаб:")
        lbl_school.setStyleSheet("color: #94A3B8; font-weight: bold; font-size: 13px;")
        self.sidebar_controls_grid.addWidget(lbl_school, 0, 0)
        
        self.school_combo = QComboBox()
        self.school_combo.setStyleSheet("""
            QComboBox { 
                background-color: #1E293B; 
                color: white; 
                border: 1px solid #334155; 
                padding: 8px; 
                border-radius: 6px; 
                font-size: 13px; 
            }
            QComboBox::drop-down { border: none; }
            QComboBox QAbstractItemView {
                background-color: #1E293B;
                color: white;
                selection-background-color: #38BDF8;
                selection-color: #0F172A;
                outline: none;
                border: 1px solid #334155;
            }
        """)
        self.school_combo.currentTextChanged.connect(self.load_classes)
        self.sidebar_controls_grid.addWidget(self.school_combo, 0, 1)
        
        lbl_class = QLabel("Синф:")
        lbl_class.setStyleSheet("color: #94A3B8; font-weight: bold; font-size: 13px;")
        self.sidebar_controls_grid.addWidget(lbl_class, 1, 0)
        
        self.class_combo = QComboBox()
        self.class_combo.setStyleSheet("""
            QComboBox { 
                background-color: #1E293B; 
                color: white; 
                border: 1px solid #334155; 
                padding: 8px; 
                border-radius: 6px; 
                font-size: 13px; 
            }
            QComboBox::drop-down { border: none; }
            QComboBox QAbstractItemView {
                background-color: #1E293B;
                color: white;
                selection-background-color: #38BDF8;
                selection-color: #0F172A;
                outline: none;
                border: 1px solid #334155;
            }
            QComboBox QAbstractItemView::item {
                min-height: 35px;
                padding-left: 8px;
            }
        """)
        self.class_combo.currentTextChanged.connect(self.load_students)
        self.sidebar_controls_grid.addWidget(self.class_combo, 1, 1)

        lbl_period = QLabel("Давра:")
        lbl_period.setStyleSheet("color: #94A3B8; font-weight: bold; font-size: 13px;")
        self.sidebar_controls_grid.addWidget(lbl_period, 2, 0)
        
        self.period_combo = QComboBox()
        self.period_combo.addItems([
            "Холҳои ҷорӣ (Онлайн)",
            "Чоряки 1",
            "Чоряки 2",
            "Нимсолаи 1",
            "Чоряки 3",
            "Чоряки 4",
            "Нимсолаи 2",
            "Солона",
            "Ҷамъбастӣ"
        ])
        self.period_combo.setStyleSheet("""
            QComboBox { 
                background-color: #1E293B; 
                color: white; 
                border: 1px solid #334155; 
                padding: 8px; 
                border-radius: 6px; 
                font-size: 13px; 
            }
            QComboBox::drop-down { border: none; }
            QComboBox QAbstractItemView {
                background-color: #1E293B;
                color: white;
                selection-background-color: #38BDF8;
                selection-color: #0F172A;
                outline: none;
                border: 1px solid #334155;
            }
            QComboBox QAbstractItemView::item {
                min-height: 35px;
                padding-left: 8px;
            }
        """)
        self.period_combo.currentTextChanged.connect(self.refresh_analytics)
        self.sidebar_controls_grid.addWidget(self.period_combo, 2, 1)

        self.sidebar_controls_grid.setColumnStretch(0, 1)
        self.sidebar_controls_grid.setColumnStretch(1, 2)
        self.sidebar_layout.addLayout(self.sidebar_controls_grid)

        self.lbl_class_stats = QLabel("🏆 Ҷойи мактабӣ: -")
        self.lbl_class_stats.setStyleSheet("color: #FBBF24; font-weight: bold; font-size: 15px; margin-top: 5px;")
        self.sidebar_layout.addWidget(self.lbl_class_stats)

        self.sidebar_layout.addSpacing(15)
        lbl_students = QLabel("РӮЙХАТИ ХОНАНДАГОН:")
        lbl_students.setStyleSheet("color: #94A3B8; font-weight: bold; font-size: 14px; letter-spacing: 1px;")
        self.sidebar_layout.addWidget(lbl_students)
        
        self.student_list = QListWidget()
        self.student_list.setStyleSheet("""
            QListWidget { background-color: transparent; border: none; color: #E2E8F0; font-size: 13px; outline: none; }
            QListWidget::item { padding: 3px 6px; border-radius: 6px; margin-bottom: 2px; }
            QListWidget::item:selected { background-color: #38BDF8; color: #0F172A; font-weight: bold; }
            QListWidget::item:hover { background-color: #1E293B; }
            QScrollBar:vertical { border: none; background: #0F172A; width: 10px; margin: 0px; }
            QScrollBar::handle:vertical { background: #334155; min-height: 30px; border-radius: 5px; }
            QScrollBar::handle:vertical:hover { background: #38BDF8; }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0px; }
        """)
        self.student_list.currentItemChanged.connect(self.display_data_nav)
        self.student_list.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.student_list.setMinimumHeight(120)
        self.sidebar_layout.addWidget(self.student_list, 1)

        # ── Anchor buttons at the bottom of the sidebar ─────────────
        

        self.btn_import_analytics = QPushButton("🔄 НАВСОЗӢ АЗ EXCEL")
        self.btn_import_analytics.setStyleSheet(
            "background-color: #0284C7; color: white; padding: 12px; "
            "font-weight: bold; font-size: 14px; border-radius: 8px;"
        )
        self.btn_import_analytics.clicked.connect(self.import_data_analytics)
        self.sidebar_layout.addWidget(self.btn_import_analytics)

        self.sidebar_layout.addSpacing(6)

        self.btn_slideshow = QPushButton("▶️ СЛАЙД-ШОУ")
        self.btn_slideshow.setStyleSheet("""
            QPushButton {
                background-color: #10B981; color: white; padding: 12px; font-size: 14px;
                font-weight: bold; border-radius: 8px;
            }
            QPushButton:hover { background-color: #059669; }
        """)
        self.btn_slideshow.clicked.connect(self.toggle_slideshow)
        self.sidebar_layout.addWidget(self.btn_slideshow)
        
        analytics_main_layout = QHBoxLayout()
        analytics_main_layout.addWidget(self.sidebar)

        # ------------------------------------------------------
        # ПАНЕЛИ РОСТ (CONTENT AREA)
        # ------------------------------------------------------
        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setStyleSheet("QScrollArea { border: none; background-color: transparent; }")
        
        self.content = QWidget()
        self.scroll_area.setWidget(self.content)
        
        self.content_layout = QVBoxLayout(self.content)
        self.content_layout.setContentsMargins(20, 10, 20, 10)
        self.content_layout.setSpacing(8)

        self.profile_card = QFrame()
        self.profile_card.setMinimumHeight(150)
        self.profile_card.setMaximumHeight(200)
        self.profile_card.setStyleSheet("background-color: white; border-radius: 20px;")
        
        from PyQt6.QtWidgets import QGraphicsDropShadowEffect
        from PyQt6.QtGui import QColor
        shadow1 = QGraphicsDropShadowEffect()
        shadow1.setBlurRadius(25)
        shadow1.setYOffset(8)
        shadow1.setColor(QColor(0, 0, 0, 35))
        self.profile_card.setGraphicsEffect(shadow1)

        self.profile_inner_layout = QHBoxLayout(self.profile_card)
        self.profile_inner_layout.setContentsMargins(15, 10, 15, 10)
        
        self.photo_box = QVBoxLayout()
        self.photo_label = ResponsiveImage() 
        self.photo_label.setFixedSize(140, 165)
        
        self.btn_upload = QPushButton("📷 Боргирии акс")
        self.btn_upload.setStyleSheet("background-color: #10B981; color: white; border-radius: 6px; font-weight: bold; padding: 6px;")
        self.btn_upload.clicked.connect(self.upload_photo_analytics)
        
        self.photo_box.addWidget(self.photo_label)
        self.photo_box.addWidget(self.btn_upload)
        self.photo_widget_container = QWidget()
        self.photo_widget_container.setLayout(self.photo_box)
        self.photo_widget_container.setMaximumWidth(140)
        self.photo_widget_container.setMaximumHeight(200)
        self.profile_inner_layout.addWidget(self.photo_widget_container)

        self.info_box = QVBoxLayout()
        self.lbl_name = QLabel("Хонандаро интихоб кунед")
        self.lbl_name.setStyleSheet("font-size: 20px; font-weight: 800; color: #0F172A; margin: 1px;")
        self.lbl_name.setWordWrap(True)

        self.lbl_avg = QLabel("📊 Холи миёна: -")
        self.lbl_avg.setStyleSheet("font-size: 13px; color: #64748B; font-weight: bold; margin: 1px;")
        self.lbl_class_rank = QLabel("👥 Ҷойи синфӣ: -")
        self.lbl_class_rank.setStyleSheet("font-size: 13px; color: #0369A1; font-weight: bold; margin: 1px;")
        self.lbl_school_rank = QLabel("🏆 Ҷойи мактабӣ: -")
        self.lbl_school_rank.setStyleSheet("font-size: 13px; color: #0369A1; font-weight: bold; margin: 1px;")
        self.lbl_district_rank = QLabel("🏆 Ҷойи ноҳиявӣ: -")
        self.lbl_district_rank.setStyleSheet("font-size: 13px; color: #d35400; font-weight: bold; margin: 1px;")
        
        self.info_box.addWidget(self.lbl_name)
        self.info_box.addWidget(self.lbl_avg)
        self.info_box.addWidget(self.lbl_class_rank)
        self.info_box.addWidget(self.lbl_school_rank)
        self.info_box.addWidget(self.lbl_district_rank)
        self.info_box.addStretch()
        self.profile_inner_layout.addLayout(self.info_box, 1)

        self.badge_box = QVBoxLayout()
        self.badge_box.setAlignment(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight)
        self.lbl_badge_icon = QLabel("🌟")
        self.lbl_badge_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_badge_icon.setStyleSheet("font-size: 40px; background-color: transparent;")
        self.lbl_badge_text = QLabel("АЪЛОЧӢ")
        self.lbl_badge_text.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_badge_text.setStyleSheet("font-size: 16px; font-weight: 900; color: #10B981;")
        self.badge_box.addWidget(self.lbl_badge_icon)
        self.badge_box.addWidget(self.lbl_badge_text)
        self.badge_widget = QWidget()
        self.badge_widget.setLayout(self.badge_box)
        self.badge_widget.setMinimumWidth(180)
        self.profile_inner_layout.addWidget(self.badge_widget)
        self.content_layout.addWidget(self.profile_card)

        self.graph_card = QFrame()
        self.graph_card.setStyleSheet("background-color: white; border-radius: 24px;")
        self.graph_card.setMaximumHeight(260)
        shadow2 = QGraphicsDropShadowEffect()
        shadow2.setBlurRadius(25)
        shadow2.setYOffset(8)
        shadow2.setColor(QColor(0, 0, 0, 35))
        self.graph_card.setGraphicsEffect(shadow2)

        self.graph_layout = QVBoxLayout(self.graph_card)
        self.graph_layout.setContentsMargins(6, 6, 6, 6)
        self.graph_layout.setSpacing(0)
        self.figure, self.ax = plt.subplots(figsize=(10, 2.5))
        self.canvas = FigureCanvas(self.figure)
        self.canvas.setMaximumHeight(260)
        self.graph_layout.addWidget(self.canvas)
        self.content_layout.addWidget(self.graph_card)

        analytics_main_layout.addWidget(self.scroll_area, 1)
        self.tab3_layout.addLayout(analytics_main_layout)

        self.slide_timer = QTimer()
        self.slide_timer.timeout.connect(self.random_student_slide)
        self.is_slideshow_active = False
        
        self.load_schools()

    def import_data_analytics(self):
        if self.analytics_db.import_excel('excel_files'):
            QMessageBox.information(self, "Омода", "Маълумот бомуваффақият навсозӣ шуд!")
            self.load_classes()
            self.refresh_tab2_data()
        else:
            QMessageBox.warning(self, "Хатогӣ", "Дар папкаи 'excel_files' файлҳои Excel (.xlsx) ёфт нашуданд!")


    def load_schools(self):
        if not hasattr(self, 'school_combo'): return
        self.school_combo.blockSignals(True)
        self.school_combo.clear()
        cursor = self.analytics_db.conn.cursor()
        try:
            cursor.execute("""
                SELECT DISTINCT school_name FROM students 
                WHERE school_name IS NOT NULL AND school_name != 'nan' AND school_name != ''
                ORDER BY school_name
            """)
            schools = [r[0] for r in cursor.fetchall()]
        except:
            schools = []
        if schools:
            self.school_combo.addItems(schools)
        self.school_combo.blockSignals(False)
        
        if schools:
            self.load_classes()

    def load_classes(self):
        self.class_combo.blockSignals(True)
        self.class_combo.clear()
        if not hasattr(self, 'school_combo'): return
        school_name = self.school_combo.currentText()
        if not school_name:
            self.class_combo.blockSignals(False)
            return
            
        cursor = self.analytics_db.conn.cursor()
        cursor.execute("""
            SELECT DISTINCT class_name FROM students 
            WHERE school_name = ? AND class_name IS NOT NULL AND class_name != 'nan' AND class_name != ''
            ORDER BY class_name
        """, (school_name,))
        classes = [r[0] for r in cursor.fetchall()]
        if classes:
            self.class_combo.addItem("⭐ Аълочиёни мактаб (10 хол)")
            self.class_combo.addItems(classes)
        self.class_combo.blockSignals(False)
        
        if classes:
            self.load_students(self.class_combo.currentText())

    def load_students(self, class_name):
        self.student_list.clear()
        if not class_name or not hasattr(self, 'school_combo'): 
            if hasattr(self, 'lbl_class_stats'):
                self.lbl_class_stats.setText("🏆 Ҷойи мактабӣ: -")
            return
            
        school_name = self.school_combo.currentText()
        cursor = self.analytics_db.conn.cursor()
        
        if class_name == "⭐ Аълочиёни мактаб (10 хол)":
            if hasattr(self, 'lbl_class_stats'):
                self.lbl_class_stats.setText("🏆 Ҷойи мактабӣ: Беҳтаринҳои мактаб")
            try:
                cursor.execute("""
                    SELECT s.full_name 
                    FROM students s
                    JOIN grades g ON s.id = g.student_id
                    WHERE s.school_name = ?
                    GROUP BY s.id
                    HAVING ROUND(AVG(g.score), 4) = 10.0
                    ORDER BY s.full_name
                """, (school_name,))
                for row in cursor.fetchall():
                    self.student_list.addItem(row[0])
            except Exception as e:
                print(f"Аълочиларни юклаш хатоси: {e}")
        else:
            try:
                if hasattr(self, 'lbl_class_stats'):
                    cursor.execute("""
                        SELECT s.class_name, ROUND(AVG(g.score), 4) as class_avg 
                        FROM grades g
                        JOIN students s ON g.student_id = s.id
                        WHERE s.school_name = ?
                        GROUP BY s.class_name
                        ORDER BY class_avg DESC
                    """, (school_name,))
                    class_rankings = cursor.fetchall()
                    
                    rank = "-"
                    avg_s = "-"
                    for index, (c_name, c_avg) in enumerate(class_rankings):
                        if c_name == class_name:
                            rank = index + 1
                            avg_s = c_avg
                            break
                            
                    if rank != "-":
                        school_type_label = "литсей" if ("литсей" in school_name.lower() or "лицей" in school_name.lower()) else "мактаб"
                        self.lbl_class_stats.setText(f"🏆 Дар {school_type_label}: ҷойи {rank} (Миёна: {avg_s:.2f})")
                    else:
                        school_type_label = "литсей" if ("литсей" in school_name.lower() or "лицей" in school_name.lower()) else "мактаб"
                        self.lbl_class_stats.setText(f"🏆 Ҷойи {school_type_label}: баҳо нест")
            except Exception as e:
                print(f"Хатогии рейтинги синф: {e}")
                if hasattr(self, 'lbl_class_stats'):
                    self.lbl_class_stats.setText("🏆 Ҷойи мактабӣ: -")
    
            cursor.execute("SELECT full_name FROM students WHERE class_name=? AND school_name=? ORDER BY full_name", (class_name, school_name))
            for row in cursor.fetchall():
                self.student_list.addItem(row[0])
        
        if self.student_list.count() > 0:
            self.student_list.setCurrentRow(0)
            self.student_list.setFocus()

    def display_data_nav(self, current, previous):
        if current:
            if getattr(self, 'is_slideshow_active', False) and not getattr(self, 'auto_changing', False):
                self.toggle_slideshow()
            self.display_data(current)

    def refresh_analytics(self):
        current_item = self.student_list.currentItem()
        school_name = self.school_combo.currentText()
        class_name = self.class_combo.currentText()
        if school_name and class_name:
            self.load_students(class_name)
            if current_item:
                items = self.student_list.findItems(current_item.text(), Qt.MatchFlag.MatchExactly)
                if items:
                    self.student_list.setCurrentItem(items[0])

    def display_data(self, item):
        if not item: return
        try:
            name = item.text()
            sinf = self.class_combo.currentText()
            school_name = self.school_combo.currentText()
            period = self.period_combo.currentText()
            
            cursor = self.analytics_db.conn.cursor()
            
            if sinf == "⭐ Аълочиёни мактаб (10 хол)":
                cursor.execute("SELECT id, class_name FROM students WHERE full_name=? AND school_name=?", (name, school_name))
                res = cursor.fetchone()
                if res:
                    student_id = res[0]
                    real_sinf = res[1]
                else:
                    return
            else:
                student_id = f"{school_name}__{sinf}__{name}"
                real_sinf = sinf
            
            # Dual query logic: live vs archived
            if period == "Холҳои ҷорӣ (Онлайн)":
                cursor.execute("SELECT subject, score FROM grades WHERE student_id=?", (student_id,))
            else:
                # For archived periods, query with period filter
                cursor.execute("SELECT subject, score FROM grades WHERE student_id=? AND period=?", (student_id, period))
            data = cursor.fetchall()
            
            self.lbl_name.setText(name)
            
            if data:
                scores = [d[1] for d in data]
                subjects = [d[0] for d in data]
                current_avg = round(sum(scores) / len(scores), 4)
                
                self.lbl_avg.setText(f"📊 Холи миёна: {current_avg:.2f}")

                # School rank
                try:
                    if period == "Холҳои ҷорӣ (Онлайн)":
                        cursor.execute("""
                            SELECT COUNT(*) FROM (
                                SELECT ROUND(AVG(g.score), 4) as avg_s FROM grades g
                                JOIN students s ON g.student_id = s.id
                                WHERE s.school_name = ?
                                GROUP BY g.student_id 
                                HAVING avg_s > ?
                            )""", (school_name, current_avg))
                    else:
                        cursor.execute("""
                            SELECT COUNT(*) FROM (
                                SELECT ROUND(AVG(g.score), 4) as avg_s FROM grades g
                                JOIN students s ON g.student_id = s.id
                                WHERE s.school_name = ? AND g.period = ?
                                GROUP BY g.student_id 
                                HAVING avg_s > ?
                            )""", (school_name, period, current_avg))
                    higher_scores_school = cursor.fetchone()[0]
                    school_type_label = "литсейӣ" if ("литсей" in school_name.lower() or "лицей" in school_name.lower()) else "мактабӣ"
                    self.lbl_school_rank.setText(f"🏆 Ҷойи {school_type_label}: {higher_scores_school + 1}")
                except Exception:
                    school_type_label = "литсейӣ" if ("литсей" in school_name.lower() or "лицей" in school_name.lower()) else "мактабӣ"
                    self.lbl_school_rank.setText(f"🏆 Ҷойи {school_type_label}: -")

                # Class rank
                try:
                    if period == "Холҳои ҷорӣ (Онлайн)":
                        cursor.execute("""
                            SELECT COUNT(*) FROM (
                                SELECT ROUND(AVG(g.score), 4) as avg_s FROM grades g
                                JOIN students s ON g.student_id = s.id
                                WHERE s.class_name = ? AND s.school_name = ?
                                GROUP BY g.student_id 
                                HAVING avg_s > ?
                            )""", (real_sinf, school_name, current_avg))
                    else:
                        cursor.execute("""
                            SELECT COUNT(*) FROM (
                                SELECT ROUND(AVG(g.score), 4) as avg_s FROM grades g
                                JOIN students s ON g.student_id = s.id
                                WHERE s.class_name = ? AND s.school_name = ? AND g.period = ?
                                GROUP BY g.student_id 
                                HAVING avg_s > ?
                            )""", (real_sinf, school_name, period, current_avg))
                    higher_scores_class = cursor.fetchone()[0]
                    self.lbl_class_rank.setText(f"👥 Ҷойи синфӣ: {higher_scores_class + 1}")
                except Exception:
                    self.lbl_class_rank.setText("👥 Ҷойи синфӣ: -")

                # District rank
                try:
                    if period == "Холҳои ҷорӣ (Онлайн)":
                        cursor.execute("""
                            SELECT COUNT(*) FROM (
                                SELECT ROUND(AVG(g.score), 4) as avg_s FROM grades g
                                GROUP BY g.student_id 
                                HAVING avg_s > ?
                            )""", (current_avg,))
                    else:
                        cursor.execute("""
                            SELECT COUNT(*) FROM (
                                SELECT ROUND(AVG(g.score), 4) as avg_s FROM grades g
                                WHERE g.period = ?
                                GROUP BY g.student_id 
                                HAVING avg_s > ?
                            )""", (period, current_avg))
                    higher_scores_district = cursor.fetchone()[0]
                    district_rank = higher_scores_district + 1
                    
                    # Add district rank to profile card
                    self.lbl_district_rank.setText(f"<p style='font-size: 12px; color: #333; margin: 1px 0;'>🏆 <b>Ҷойи ноҳиявӣ:</b> <span style='color: #d35400; font-weight: bold;'>{district_rank}</span></p>")
                except Exception:
                    self.lbl_district_rank.setText("<p style='font-size: 12px; color: #333; margin: 1px 0;'>🏆 <b>Ҷойи ноҳиявӣ:</b> <span style='color: #d35400; font-weight: bold;'>-</span></p>")

                if current_avg >= 9:
                    b_icon, b_txt, b_col = "🏆", "АЪЛОЧӢ", "#10B981"
                elif current_avg >= 7:
                    b_icon, b_txt, b_col = "👍", "ХУБ", "#3B82F6"
                elif current_avg >= 4:
                    b_icon, b_txt, b_col = "😐", "МИЁНА", "#F59E0B"
                else:
                    b_icon, b_txt, b_col = "📉", "СУСТ", "#EF4444"
                
                self.lbl_badge_icon.setText(b_icon)
                self.lbl_badge_text.setText(b_txt)
                self.lbl_badge_text.setStyleSheet(f"font-size: 16px; font-weight: 900; color: {b_col}; margin: 0px;")

                self.draw_chart(subjects, scores)
            else:
                self.lbl_avg.setText("📊 Холи миёна: -")
                self.lbl_badge_icon.setText("")
                self.lbl_badge_text.setText("")
                self.lbl_class_rank.setText("👥 Ҷойи синфӣ: -")
                school_type_label = "литсейӣ" if ("литсей" in school_name.lower() or "лицей" in school_name.lower()) else "мактабӣ"
                self.lbl_school_rank.setText(f"🏆 Ҷойи {school_type_label}: -")
                self.lbl_district_rank.setText("<p style='font-size: 12px; color: #333; margin: 1px 0;'>🏆 <b>Ҷойи ноҳиявӣ:</b> <span style='color: #d35400; font-weight: bold;'>-</span></p>")
                self.ax.clear()
                self.canvas.draw()

            photo_path = f"photos/{student_id}.jpg"
            default_path = "photos/default.jpg"

            if os.path.exists(photo_path):
                display_path = photo_path
            elif os.path.exists(default_path):
                display_path = default_path
            else:
                display_path = None

            if display_path:
                pixmap = QPixmap(display_path)
                if not pixmap.isNull():
                    self.photo_label.setPixmapOriginal(pixmap)
                    self.photo_label.setText("")
                else:
                    self.photo_label.setPixmapOriginal(None)
                    self.photo_label.setText("ХАТО")
            else:
                self.photo_label.setPixmapOriginal(None)
                self.photo_label.clear()
                self.photo_label.setText("АКС НЕСТ")

        except Exception as e:
            print(f"Маълумот чиқаришда хатолик: {e}")
            self.lbl_avg.setText("📊 Холи миёна: -")
            self.lbl_badge_icon.setText("")
            self.lbl_badge_text.setText("")
            self.lbl_class_rank.setText("👥 Ҷойи синфӣ: -")
            self.lbl_school_rank.setText("🏆 Ҷойи мактабӣ: -")
            self.lbl_district_rank.setText("<p style='font-size: 12px; color: #333; margin: 1px 0;'>🏆 <b>Ҷойи ноҳиявӣ:</b> <span style='color: #d35400; font-weight: bold;'>-</span></p>")
            self.ax.clear()
            self.canvas.draw()

    def refresh_tab2_data(self):
        try:
            # Load statistics from main database using QSqlQuery
            query = QSqlQuery()
            
            # Institution count
            query.exec("SELECT COUNT(*) FROM schools")
            inst_count = 0
            if query.next():
                inst_count = query.value(0)
            self.lbl_institutions_count.setText(str(inst_count))
            
            # Students count (actual registered students from analytics database)
            cursor = self.analytics_db.conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM students")
            students_count = cursor.fetchone()[0]
            self.lbl_students_count.setText(str(students_count))
            
            # Staff count
            query.exec("SELECT COUNT(*) FROM teachers")
            staff_count = 0
            if query.next():
                staff_count = query.value(0)
            self.lbl_staff_count.setText(str(staff_count))
            
            # Ratio calculation
            if staff_count > 0:
                ratio = students_count / staff_count
                self.lbl_ratio.setText(f"{ratio:.1f}")
            else:
                self.lbl_ratio.setText("0.0")
            
            # Populate school filter for class rankings
            try:
                cursor.execute("SELECT DISTINCT school_name FROM students ORDER BY school_name")
                schools = [r[0] for r in cursor.fetchall()]
                current_filter = self.combo_rank_school_filter.currentText()
                self.combo_rank_school_filter.blockSignals(True)
                self.combo_rank_school_filter.clear()
                self.combo_rank_school_filter.addItem("Ҳамаи мактабҳо")
                self.combo_rank_school_filter.addItems(schools)
                # Restore previous selection if still available
                index = self.combo_rank_school_filter.findText(current_filter)
                if index >= 0:
                    self.combo_rank_school_filter.setCurrentIndex(index)
                self.combo_rank_school_filter.blockSignals(False)
            except Exception as e:
                print(f"Хатогии бор кардани филтри мактабҳо: {e}")
            
            # Calculate school rankings
            self.calculate_school_rankings()
            
            # Calculate class rankings
            self.calculate_class_rankings()
            
            # Calculate subject rankings
            self.calculate_subject_rankings()
            
        except Exception as e:
            print(f"Хатогии бор кардани омори Tab 2: {e}")

    def load_tab2_statistics(self):
        self.refresh_tab2_data()

    def calculate_class_rankings(self):
        try:
            school_filter = self.combo_rank_school_filter.currentText()
            
            cursor = self.analytics_db.conn.cursor()
            
            # Fetch all classes sorted by GPA descending for district-wide ranking
            cursor.execute("""
                SELECT school_name, class_name, ROUND(AVG(score), 2) as class_avg
                FROM grades g
                JOIN students s ON g.student_id = s.id
                GROUP BY school_name, class_name
                ORDER BY class_avg DESC
            """)
            
            all_results = cursor.fetchall()
            
            # Compute district rank and per-school rank in a single pass
            school_counters = {}
            ranked_results = []
            for district_rank, (school, class_name, avg_score) in enumerate(all_results, start=1):
                school_counters[school] = school_counters.get(school, 0) + 1
                school_rank = school_counters[school]
                ranked_results.append({
                    'district_rank': district_rank,
                    'school_rank': school_rank,
                    'school': school,
                    'class_name': class_name,
                    'avg_score': avg_score
                })
            
            # Apply school filter while preserving exact district-wide rank
            if school_filter and school_filter != "Ҳамаи мактабҳо":
                filtered_results = [r for r in ranked_results if r['school'] == school_filter]
            else:
                filtered_results = ranked_results
            
            # Populate the table
            self.table_class_rankings.setRowCount(len(filtered_results))
            for row_idx, r in enumerate(filtered_results):
                self.table_class_rankings.setItem(row_idx, 0, QTableWidgetItem(str(r['district_rank'])))
                self.table_class_rankings.setItem(row_idx, 1, QTableWidgetItem(r['school']))
                self.table_class_rankings.setItem(row_idx, 2, QTableWidgetItem(r['class_name']))
                self.table_class_rankings.setItem(row_idx, 3, QTableWidgetItem(str(r['school_rank'])))
                self.table_class_rankings.setItem(row_idx, 4, QTableWidgetItem(str(r['district_rank'])))
                self.table_class_rankings.setItem(row_idx, 5, QTableWidgetItem(str(r['avg_score'])))
            
            self.table_class_rankings.resizeColumnsToContents()
            
        except Exception as e:
            print(f"Хатогии ҳисоб кардани рейтинги синфҳо: {e}")

    def calculate_subject_rankings(self):
        try:
            cursor = self.analytics_db.conn.cursor()
            
            cursor.execute("""
                SELECT subject, ROUND(AVG(score), 2) as sub_avg
                FROM grades
                GROUP BY subject
                ORDER BY sub_avg DESC
            """)
            
            results = cursor.fetchall()
            
            self.table_subject_rankings.setRowCount(len(results))
            for row_idx, (subject, avg_score) in enumerate(results):
                self.table_subject_rankings.setItem(row_idx, 0, QTableWidgetItem(str(row_idx + 1)))
                self.table_subject_rankings.setItem(row_idx, 1, QTableWidgetItem(subject))
                self.table_subject_rankings.setItem(row_idx, 2, QTableWidgetItem(str(avg_score)))
            
            self.table_subject_rankings.resizeColumnsToContents()
            
        except Exception as e:
            print(f"Хатогии ҳисоб кардани рейтинги фанҳо: {e}")

    def calculate_school_rankings(self):
        try:
            cursor = self.analytics_db.conn.cursor()
            
            cursor.execute("""
                SELECT s.school_name, ROUND(AVG(g.score), 2) as school_avg
                FROM grades g
                JOIN students s ON g.student_id = s.id
                GROUP BY s.school_name
                ORDER BY school_avg DESC
            """)
            
            results = cursor.fetchall()
            
            self.table_school_rankings.setRowCount(len(results))
            for row_idx, (school, avg_score) in enumerate(results):
                self.table_school_rankings.setItem(row_idx, 0, QTableWidgetItem(str(row_idx + 1)))
                self.table_school_rankings.setItem(row_idx, 1, QTableWidgetItem(school))
                self.table_school_rankings.setItem(row_idx, 2, QTableWidgetItem(str(avg_score)))
            
            self.table_school_rankings.resizeColumnsToContents()
            
        except Exception as e:
            print(f"Хатогии ҳисоб кардани рейтинги муассисаҳо: {e}")

    def upload_photo_analytics(self):
        current_item = self.student_list.currentItem()
        if not current_item:
            QMessageBox.warning(self, "Таваҷҷӯҳ", "Аввал хонандаро аз рӯйхат интихоб кунед!")
            return

        name = current_item.text()
        sinf = self.class_combo.currentText()
        school_name = self.school_combo.currentText()
        
        if sinf == "⭐ Аълочиёни мактаб (10 хол)":
            cursor = self.analytics_db.conn.cursor()
            cursor.execute("SELECT id FROM students WHERE full_name=? AND school_name=?", (name, school_name))
            res = cursor.fetchone()
            if res:
                student_id = res[0]
            else:
                return
        else:
            student_id = f"{school_name}__{sinf}__{name}"

        file_path, _ = QFileDialog.getOpenFileName(self, "Интихоби акс", "", "Images (*.jpg *.png *.jpeg)")
        if file_path:
            try:
                if not os.path.exists('photos'): 
                    os.makedirs('photos')
                target_path = f"photos/{student_id}.jpg"
                
                shutil.copy2(file_path, target_path)
                
                self.display_data(current_item) 
                QMessageBox.information(self, "Омода", "Акdef load_schools(self): бомуваффақият захира шуд!")
            except Exception as e:
                QMessageBox.critical(self, "Хатогӣ", f"Хато ҳангоми захираи ё намоиши акс: {e}")

    def toggle_slideshow(self):
        if not self.is_slideshow_active:
            current_class = self.class_combo.currentText()
            cursor = self.analytics_db.conn.cursor()
            
            if current_class == "⭐ Аълочиёни мактаб (10 хол)":
                cursor.execute("""
                    SELECT s.full_name, s.class_name 
                    FROM students s
                    JOIN grades g ON s.id = g.student_id
                    GROUP BY s.id
                    HAVING ROUND(AVG(g.score), 4) = 10.0
                    ORDER BY s.full_name
                """)
            else:
                cursor.execute("""
                    SELECT full_name, class_name FROM students 
                    WHERE class_name = ?
                      AND full_name NOT LIKE '%столбец%' 
                      AND full_name NOT LIKE '%Unnamed%'
                      AND full_name != 'nan'
                      AND full_name != ''
                    ORDER BY full_name
                """, (current_class,))
                
            self.all_students_cache = cursor.fetchall()
            
            if not self.all_students_cache:
                QMessageBox.warning(self, "Хатогӣ", "Базаи маълумотҳо холӣ аст ё маълумоти нодуруст!")
                return

            self.current_slide_index = 0
            self.slide_timer.start(8000)
            self.btn_slideshow.setText("⏹️ ҚАТЪИ СЛАЙД")
            self.btn_slideshow.setStyleSheet("background-color: #EF4444; color: white; padding: 12px; font-size: 14px; font-weight: bold; border-radius: 8px;")
            self.is_slideshow_active = True
            self.random_student_slide()
        else:
            self.slide_timer.stop()
            self.btn_slideshow.setText("▶️ СЛАЙД-ШОУ")
            self.btn_slideshow.setStyleSheet("background-color: #10B981; color: white; padding: 12px; font-size: 14px; font-weight: bold; border-radius: 8px;")
            self.is_slideshow_active = False

    def random_student_slide(self):
        if not hasattr(self, 'current_slide_index'):
            self.current_slide_index = 0
            
        if not self.all_students_cache: return
        
        self.auto_changing = True
        
        student = self.all_students_cache[self.current_slide_index]
        name, sinf = student

        self.current_slide_index += 1
        if self.current_slide_index >= len(self.all_students_cache):
            self.current_slide_index = 0

        if self.class_combo.currentText() != "⭐ Аълочиёни мактаб (10 хол)":
            if self.class_combo.currentText() != sinf:
                self.class_combo.setCurrentText(sinf)
        
        items = self.student_list.findItems(name, Qt.MatchFlag.MatchExactly)
        if items:
            self.student_list.setCurrentItem(items[0])
            
        self.auto_changing = False

    def draw_chart(self, subjects, scores):
        self.ax.clear()
        self.ax.yaxis.grid(True, linestyle='--', alpha=0.3, color='#CBD5E1')
        self.ax.set_axisbelow(True)
        
        colors = ['#22C55E' if s >= 9 else '#3B82F6' if s >= 7 else '#EAB308' if s >= 5 else '#EF4444' for s in scores]
        bars = self.ax.bar(subjects, scores, color=colors, width=0.6)
        
        self.ax.set_ylim(0, 11)
        self.ax.set_title("Нишондиҳандаҳо аз рӯи фанҳо", fontsize=12, fontweight='bold', pad=15)
        
        self.ax.set_xticks(range(len(subjects)))
        self.ax.set_xticklabels(subjects, rotation=45, ha='right', fontsize=9)
        
        for bar in bars:
            height = bar.get_height()
            self.ax.text(bar.get_x() + bar.get_width()/2., height + 0.2, f'{height:.1f}', 
                         ha='center', va='bottom', color='#1E293B', fontweight='bold', fontsize=10)

        self.ax.spines['top'].set_visible(False)
        self.ax.spines['right'].set_visible(False)
        self.figure.subplots_adjust(bottom=0.35, left=0.06, right=0.98, top=0.78)
        self.canvas.draw()

if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = SchoolManagementSystem()
    window.showMaximized()
    sys.exit(app.exec())