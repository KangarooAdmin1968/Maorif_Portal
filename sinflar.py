from PyQt6.QtCore import Qt
from PyQt6.QtCore import QSize 
import sys
import os # Файл тизими билан ишлаш учун
import shutil # Файлларни кўчириш учун
from PyQt6.QtGui import QPixmap # Расмларни кўрсатиш учун
from PyQt6.QtWidgets import QFileDialog # Файл танлаш ойнасини очиш учун
import json
import sqlite3
from datetime import datetime
from PyQt6.QtCore import Qt, pyqtSignal 
from PyQt6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QComboBox,
    QTableWidget, QTableWidgetItem, QHeaderView, QMessageBox,
    QDialog, QCalendarWidget, QInputDialog
)
from PyQt6.QtGui import QFont
from PyQt6.QtCore import QDate
from ui.report_window import ReportWindow # ui/report_window.py мавжуд бўлиши керак


class CustomTableWidget(QTableWidget):
    grade_commit_required = pyqtSignal(int, int, str, str)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def keyPressEvent(self, event):
        """ Enter ва Пастга стрелка (↓) нинг ишлашини ва баҳоларни сақлашни таъминлаш. """
        
        current_item = self.currentItem()
        if not current_item:
            super().keyPressEvent(event)
            return

        key = event.key()
        current_row = current_item.row()
        current_col = current_item.column()
        
        # 1. ENTER ёки ПАСТГА СТРЕЛКА (↓) босилса:
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Down):
            
            # Агар Enter босилган бўлса, таҳрирлашни тугатамиз
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                
                saved_text = ""
                
                # 💡 ТУЗАТИШ: Таҳрирланувчи устунлар (3, 4, 5, 15) учун
                if current_col in [3, 4, 5, 15]:
                    
                    # Таҳрирлагичдан маълумотни олиш мантиғи
                    editor = self.cellWidget(current_row, current_col)
                    if editor and isinstance(editor, QLineEdit):
                        saved_text = editor.text().strip()
                    else:
                        saved_text = current_item.text().strip()

                    self.closePersistentEditor(current_item)
                    
                    if current_col in [3, 4, 5, 15]:
                        current_item.setText(saved_text)
                        current_item.setData(Qt.ItemDataRole.DisplayRole, saved_text)

                        # ✔️ StudentApp га сигнал (Сақлаш учун)
                        student_id = self.item(current_row, 1).text()
                        self.grade_commit_required.emit(current_row, current_col, student_id, saved_text)
                        
                        event.accept() # Enter нинг стандарт ҳаракатини тўлиқ блоклаш
                        
                else:
                    self.closePersistentEditor(current_item) 
                    event.ignore() 
                
                self.setFocus()
                
            
            # Кейинги қаторга ўтиш мантиғи (олдингидек)
            row_count = self.rowCount()
            next_row = current_row + 1
            
            if next_row < row_count:
                
                # Кейинги устунни аниқлаш мантиғи
                if current_col == 15:       
                    next_col = 15 
                elif current_col >= 3 and current_col <= 5:  
                    next_col = current_col
                else:                       
                    next_col = 3 
                
                next_item = self.item(next_row, next_col)
                
                if next_item:
                    self.setCurrentItem(next_item)
                
                event.accept() 
                return
            
            event.accept()
            return
            
        # 2. DELETE ёки BACKSPACE - Ячейкани тозалаш
        elif key in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            if current_item and current_col > 2:
                if current_col == 15 or current_col <= 5: 
                    current_item.setText("") 
                event.accept()
                return
        
        super().keyPressEvent(event)

def create_grades_table():
    if not os.path.exists("data"):
        os.makedirs("data")
    if not os.path.exists("data/source_files"):
        os.makedirs("data/source_files")
    with sqlite3.connect("data/sinflar.db") as conn:
        cursor = conn.cursor()

        # Асосий рӯйхати хонандагон
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS students (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                student_name TEXT UNIQUE,
                class_name TEXT
            )
        """)

        # Чоракҳои архив
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS quarter_archive (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                student_name TEXT,
                class_name TEXT,
                subject TEXT,
                quarter INTEGER,
                year TEXT,
                avg_grade REAL
            )
        """)

        # Баҳоҳо
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS grades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                student_id INTEGER,
                student_name TEXT,
                class_name TEXT,
                subject TEXT,
                date TEXT,
                grade TEXT,
                behavior INTEGER,
                attendance TEXT,
                activity_grade INTEGER,
                grade_type TEXT DEFAULT 'daily'
            )
        """)

        # Устунҳои зарурӣ (агар мавҷуд набошанд)
        for migration in [
            "ALTER TABLE grades ADD COLUMN student_id INTEGER",
            "ALTER TABLE grades ADD COLUMN grade_type TEXT DEFAULT 'daily'"
        ]:
            try:
                cursor.execute(migration)
            except sqlite3.OperationalError:
                pass

        # Пайвастагии student_id-ро бо рӯйхати хонандагон барқарор мекунад
        cursor.execute("""
            UPDATE grades
            SET student_id = (
                SELECT id FROM students
                WHERE students.student_name = grades.student_name
            )
            WHERE student_id IS NULL AND student_name IS NOT NULL
        """)

        # Ҷадвали ҳисобшудаи чоракҳо
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS quarter_grades (
                student_id INTEGER,
                class_name TEXT,
                subject TEXT,
                quarter INTEGER,
                grade INTEGER,
                att_grade INTEGER,
                UNIQUE(student_id, class_name, subject, quarter)
            )
        """)

        # Устунҳои ҷадвали quarter_grades (агар мавҷуд набошанд)
        for migration in [
            "ALTER TABLE quarter_grades ADD COLUMN grade INTEGER",
            "ALTER TABLE quarter_grades ADD COLUMN att_grade INTEGER"
        ]:
            try:
                cursor.execute(migration)
            except sqlite3.OperationalError:
                pass

        conn.commit()


# 🔧 Ёрдамчи функциялар
DATA_DIR = "data"
def load_json(filename, default):
    path = os.path.join(DATA_DIR, filename)
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except json.JSONDecodeError:
            return default
    return default

def save_json(filename, data):
    path = os.path.join(DATA_DIR, filename)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

# 🧩 Асосий интерфейс
class StudentApp(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Барномаи баҳодиҳӣ")
        self.setFont(QFont("Arial", 14))

        # Экран кенглигини автоматик созлаш
        screen = QApplication.primaryScreen().geometry()
        width = int(screen.width() * 0.95)
        height = int(screen.height() * 0.9)
        self.resize(width, height)

        self.subjects = load_json("subjects.json", [])
        self.classes = load_json("classes.json", {})
        self.current_class = None
        self.current_subject = None
        self.report_window = None
        self.current_date = datetime.now().strftime("%d.%m.%Y")
        self.active_date_label = QLabel(f"Санаи фаъол: {self.current_date}")
        self.check_and_update_db()

        # Интерфейс элементларини яратиш
        self.subject_combo = QComboBox()
        self.subject_combo.addItems(self.subjects)
        self.subject_combo.currentIndexChanged.connect(self.set_current_subject)
        self.subject_input = QLineEdit()
        self.subject_input.setPlaceholderText("Фани нав ворид кунед")
        self.add_subject_btn = QPushButton("Иловаи фан")
        self.add_subject_btn.clicked.connect(self.add_subject)
        self.remove_subject_btn = QPushButton("Ҳазфи фан")
        self.remove_subject_btn.clicked.connect(self.remove_subject)

        self.class_combo = QComboBox()
        self.class_combo.addItems(self.classes.keys())
        self.class_combo.currentIndexChanged.connect(self.on_class_or_subject_changed)
        self.class_input = QLineEdit()
        self.class_input.setPlaceholderText("Синфи нав ворид кунед")
        self.add_class_btn = QPushButton("Иловаи синф")
        self.add_class_btn.clicked.connect(self.add_class)
        self.remove_class_btn = QPushButton("Ҳазфи синф")
        self.remove_class_btn.clicked.connect(self.remove_class)

        self.student_input = QLineEdit()
        self.student_input.setPlaceholderText("Ному насаби хонандаи нав")
        self.add_student_btn = QPushButton("Иловаи хонанда")
        self.add_student_btn.clicked.connect(self.add_student)
        self.remove_student_btn = QPushButton("Ҳазфи хонанда")
        self.remove_student_btn.clicked.connect(self.remove_student)

        self.finish_quarter_btn = QPushButton("Чоракни якунлаш (Архив)")
        self.finish_quarter_btn.clicked.connect(self.finish_quarter_and_archive)

        self.edit_past_grades_btn = QPushButton("Интихоби сана")
        self.edit_past_grades_btn.clicked.connect(self.show_date_selector)

        # Расм майдони
        self.photo_label = QLabel("Акс дар ин ҷо нишон дода мешавад")
        self.photo_label.setFixedSize(120, 160)
        self.photo_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.photo_label.setStyleSheet("border: 1px solid #ccc; background-color: #f0f0f0;")
        self.upload_photo_btn = QPushButton("📷 Акс бор кардан")
        self.upload_photo_btn.clicked.connect(self.upload_photo)

        photo_controls_hlayout = QHBoxLayout()
        photo_controls_hlayout.addWidget(self.photo_label)
        photo_controls_hlayout.addSpacing(10)
        photo_controls_hlayout.addWidget(self.upload_photo_btn)
        photo_controls_hlayout.addStretch(1)

        # Жадвал
        self.table = CustomTableWidget()
        self.table.setFont(QFont("Arial", 14))
        self.table.grade_commit_required.connect(self.handle_grade_commit)
        self.table.setColumnCount(17)

        self.table.setHorizontalHeaderLabels([
            "Т/Р",
            "Рамзи ID",
            "Ному насаб",
            "Хол (10)",
            "Тарбия (5)",
            "Фаъолият (5)",
            "Давомат (%)",
            "Сана",
            "Ч1",
            "Ч2",
            "Н/с1",
            "Ч3",
            "Ч4",
            "Н/с2",
            "Солона",
            "Атт",
            "Умумӣ",
        ])

        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(6, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(7, QHeaderView.ResizeMode.ResizeToContents)
        for col in range(8, 17):
            self.table.horizontalHeader().setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)

        self.table.verticalHeader().setVisible(False)
        self.table.currentItemChanged.connect(self.display_student_photo_by_cursor)
        self.save_btn = QPushButton("Сабт кардан")
        self.save_btn.clicked.connect(self.save_grades)

        self.report_button = QPushButton("Ҳисобот")
        self.report_button.clicked.connect(self.show_report_window)

        # Интерфейс жойлашуви
        layout = QVBoxLayout()
        layout.addWidget(self.active_date_label)

        hlayout1 = QHBoxLayout()
        hlayout1.addWidget(QLabel("Фан:"))
        hlayout1.addWidget(self.subject_combo)
        hlayout1.addWidget(self.subject_input)
        hlayout1.addWidget(self.add_subject_btn)
        hlayout1.addWidget(self.remove_subject_btn)

        hlayout2 = QHBoxLayout()
        hlayout2.addWidget(QLabel("Синф:"))
        hlayout2.addWidget(self.class_combo)
        hlayout2.addWidget(self.class_input)
        hlayout2.addWidget(self.add_class_btn)
        hlayout2.addWidget(self.remove_class_btn)

        hlayout3 = QHBoxLayout()
        hlayout3.addWidget(self.student_input)
        hlayout3.addWidget(self.add_student_btn)
        hlayout3.addWidget(self.remove_student_btn)
        hlayout3.addWidget(self.finish_quarter_btn)
        hlayout3.addWidget(self.edit_past_grades_btn)
        hlayout3.addStretch(1)

        left_section_vlayout = QVBoxLayout()
        left_section_vlayout.addLayout(hlayout1)
        left_section_vlayout.addLayout(hlayout2)

        photo_vlayout = QVBoxLayout()
        photo_vlayout.addLayout(photo_controls_hlayout)

        top_wrapper_hlayout = QHBoxLayout()
        top_wrapper_hlayout.addLayout(left_section_vlayout, 3)
        top_wrapper_hlayout.addLayout(photo_vlayout, 1)

        layout.addLayout(top_wrapper_hlayout)
        layout.addLayout(hlayout3)
        layout.addWidget(self.table)

        layout.addWidget(self.save_btn)
        layout.addWidget(self.report_button)
        self.setLayout(layout)

        if self.subjects:
            self.set_current_subject()

        self.load_class_data()


# StudentApp синфи ичида

    def handle_grade_commit(self, row, col, student_id, new_grade_str):
        """CustomTableWidget дан сигнал қабул қилиб, баҳони сақлайди."""
        current_class = self.class_combo.currentText()
        subject = self.subject_combo.currentText()
        if not current_class or not subject:
            return
        if col not in [3, 4, 5, 15]:
            return

        try:
            with sqlite3.connect("data/sinflar.db") as conn:
                cursor = conn.cursor()
                name_item = self.table.item(row, 2)
                student_name = name_item.text() if name_item else ""

                cursor.execute("""
                    SELECT id FROM grades
                    WHERE student_id = ? AND class_name = ? AND subject = ? AND date = ?
                """, (student_id, current_class, subject, self.current_date))
                rec = cursor.fetchone()

                if col == 15:
                    att_val = int(new_grade_str) if new_grade_str.isdigit() else None
                    if att_val is not None:
                        cursor.execute("""
                            INSERT OR REPLACE INTO quarter_grades
                            (student_id, class_name, subject, quarter, att_grade)
                            VALUES (?, ?, ?, 0, ?)
                        """, (student_id, current_class, subject, att_val))
                    else:
                        cursor.execute("""
                            DELETE FROM quarter_grades
                            WHERE student_id = ? AND class_name = ? AND subject = ? AND quarter = 0
                        """, (student_id, current_class, subject))
                elif col == 3:
                    attendance = 'б'
                    grade = None
                    grade_type = 'daily'
                    val = new_grade_str.strip()
                    if not val:
                        if rec:
                            cursor.execute("""
                                UPDATE grades
                                SET grade = NULL, grade_type = 'daily', attendance = 'б'
                                WHERE id = ?
                            """, (rec[0],))
                    elif val.lower() == 'ғ':
                        if not rec:
                            cursor.execute("""
                                INSERT INTO grades
                                (student_id, student_name, class_name, subject, date, grade, attendance, grade_type)
                                VALUES (?, ?, ?, ?, ?, NULL, 'ғ', 'daily')
                            """, (student_id, student_name, current_class, subject, self.current_date))
                        else:
                            cursor.execute("""
                                UPDATE grades
                                SET grade = NULL, attendance = 'ғ', grade_type = 'daily'
                                WHERE id = ?
                            """, (rec[0],))
                    elif '/' in val:
                        parts = val.split('/')
                        if len(parts) == 2 and all(p.isdigit() for p in parts):
                            grade = val
                            grade_type = 'control'
                            if not rec:
                                cursor.execute("""
                                    INSERT INTO grades
                                    (student_id, student_name, class_name, subject, date, grade, attendance, grade_type)
                                    VALUES (?, ?, ?, ?, ?, ?, 'б', 'control')
                                """, (student_id, student_name, current_class, subject, self.current_date, grade))
                            else:
                                cursor.execute("""
                                    UPDATE grades
                                    SET grade = ?, attendance = 'б', grade_type = ?
                                    WHERE id = ?
                                """, (grade, grade_type, rec[0]))
                        else:
                            QMessageBox.warning(self, "Хатогӣ", "Формати баҳо нодуруст. '10/8'-ро истифода баред.")
                            return
                    elif val.isdigit():
                        grade = int(val)
                        if not rec:
                            cursor.execute("""
                                INSERT INTO grades
                                (student_id, student_name, class_name, subject, date, grade, attendance, grade_type)
                                VALUES (?, ?, ?, ?, ?, ?, 'б', 'daily')
                            """, (student_id, student_name, current_class, subject, self.current_date, grade))
                        else:
                            cursor.execute("""
                                UPDATE grades
                                SET grade = ?, attendance = 'б', grade_type = 'daily'
                                WHERE id = ?
                            """, (grade, rec[0]))
                    else:
                        QMessageBox.warning(self, "Хатогӣ", "Баҳо фақат рақам, '10/8' ёки 'ғ' бошад.")
                        return
                elif col == 4:
                    behavior = int(new_grade_str) if new_grade_str.isdigit() else None
                    if behavior is None and not rec:
                        return
                    if not rec:
                        cursor.execute("""
                            INSERT INTO grades
                            (student_id, student_name, class_name, subject, date, behavior)
                            VALUES (?, ?, ?, ?, ?, ?)
                        """, (student_id, student_name, current_class, subject, self.current_date, behavior))
                    else:
                        cursor.execute("UPDATE grades SET behavior = ? WHERE id = ?", (behavior, rec[0]))
                elif col == 5:
                    activity = int(new_grade_str) if new_grade_str.isdigit() else None
                    if activity is None and not rec:
                        return
                    if not rec:
                        cursor.execute("""
                            INSERT INTO grades
                            (student_id, student_name, class_name, subject, date, activity_grade)
                            VALUES (?, ?, ?, ?, ?, ?)
                        """, (student_id, student_name, current_class, subject, self.current_date, activity))
                    else:
                        cursor.execute("UPDATE grades SET activity_grade = ? WHERE id = ?", (activity, rec[0]))
                conn.commit()
            self.calculate_all_quarter_grades()
            self.load_class_data(date_filter=self.current_date)
        except sqlite3.Error as e:
            QMessageBox.critical(self, "Хатогӣ", f"Баҳо сабт карда нашуд: {e}")

    def set_current_subject(self):
        self.current_subject = self.subject_combo.currentText()
        # Энди load_class_data() ни эмас, балки on_class_or_subject_changed() ни чақирамиз
        self.on_class_or_subject_changed()

    def get_attendance_percentage(self, student_id, class_name):
        """Давомати хонанда барои фани ҷорӣ ҳисоб карда мешавад."""
        try:
            with sqlite3.connect("data/sinflar.db") as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT attendance
                    FROM grades
                    WHERE student_id = ? AND class_name = ? AND subject = ?
                """, (student_id, class_name, self.current_subject))
                all_attendance = cursor.fetchall()
                if not all_attendance:
                    return "100%"
                total_lessons = len(all_attendance)
                absent_count = sum(1 for (att,) in all_attendance if att and att.strip().lower() == 'ғ')
                if total_lessons == 0:
                    return "100%"
                attendance_percent = ((total_lessons - absent_count) / total_lessons) * 100
                return f"{attendance_percent:.0f}%"
        except sqlite3.Error:
            return "100%"

    def load_class_data(self, date_filter=None):
        """Маълумоти хонандагон ва баҳоҳо барои синф/фан/сана бор карда мешавад."""
        current_class = self.class_combo.currentText()
        current_subject = self.subject_combo.currentText()

        if not current_class or not current_subject:
            self.table.setRowCount(0)
            return

        if date_filter is not None:
            date_to_load = date_filter
        elif self.current_date:
            date_to_load = self.current_date
        else:
            date_to_load = datetime.now().strftime("%d.%m.%Y")

        self.current_date = date_to_load
        if hasattr(self, 'active_date_label'):
            self.active_date_label.setText(f"Санаи фаъол: {self.current_date}")

        self.table.setRowCount(0)

        try:
            with sqlite3.connect("data/sinflar.db") as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT id, student_name, class_name
                    FROM students
                    WHERE class_name = ?
                    ORDER BY student_name
                """, (current_class,))
                students_data = cursor.fetchall()

                for row_num, row_data in enumerate(students_data):
                    t_r = row_num + 1
                    student_id, student_name, _ = row_data

                    cursor.execute("""
                        SELECT grade, behavior, activity_grade, attendance, date
                        FROM grades
                        WHERE student_id = ? AND class_name = ? AND subject = ? AND date = ?
                    """, (student_id, current_class, current_subject, date_to_load))
                    today_data = cursor.fetchone()

                    if today_data:
                        grade, behavior, activity, attendance_status, date_from_db = today_data
                    else:
                        grade, behavior, activity, attendance_status, date_from_db = None, None, None, None, date_to_load

                    avg_grade_text = ""
                    if behavior is None:
                        cursor.execute("""
                            SELECT behavior FROM grades
                            WHERE student_id = ? AND class_name = ? AND behavior IS NOT NULL
                            ORDER BY id DESC LIMIT 1
                        """, (student_id, current_class))
                        last = cursor.fetchone()
                        behavior = last[0] if last else None

                    if activity is None:
                        cursor.execute("""
                            SELECT activity_grade FROM grades
                            WHERE student_id = ? AND class_name = ? AND activity_grade IS NOT NULL
                            ORDER BY id DESC LIMIT 1
                        """, (student_id, current_class))
                        last = cursor.fetchone()
                        activity = last[0] if last else None

                    grade_display = ""
                    if grade is not None:
                        grade_display = str(grade)
                    elif attendance_status and attendance_status.strip().lower() == 'ғ':
                        grade_display = 'ғ'

                    attendance_percent_text = self.get_attendance_percentage(student_id, current_class)
                    self.add_student_row(
                        t_r,
                        student_id,
                        student_name,
                        grade_display,
                        behavior,
                        activity,
                        attendance_percent_text,
                        self.current_date,
                        avg_grade_text
                    )
        except sqlite3.Error as e:
            QMessageBox.critical(self, "Хатогӣ", f"Маълумотро бор карда нашуд: {e}")
            return

        self.calculate_all_quarter_grades()

    def calculate_all_quarter_grades(self):
        """Чорак/нимсола/солона/умумӣ баҳоҳо ҳисоб карда, база ва жадвал навсозӣ мешаванд."""
        class_name = self.class_combo.currentText()
        subject = self.subject_combo.currentText()

        if not class_name or not subject:
            return

        try:
            with sqlite3.connect("data/sinflar.db") as conn:
                cursor = conn.cursor()

                for row in range(self.table.rowCount()):
                    student_id = self.table.item(row, 1).text()

                    # Баҳои ҳисобшудаи чоракҳои пешина
                    cursor.execute("""
                        SELECT quarter, grade FROM quarter_grades
                        WHERE student_id = ? AND class_name = ? AND subject = ? AND quarter IN (1,2,3,4)
                    """, (student_id, class_name, subject))
                    quarter_grades_db = {q: g for q, g in cursor.fetchall()}

                    # Ҳамаи баҳоҳо аз рӯи сана ба чоракҳо тақсим карда мешаванд
                    cursor.execute("""
                        SELECT grade, date FROM grades
                        WHERE student_id = ? AND class_name = ? AND subject = ?
                    """, (student_id, class_name, subject))
                    all_grades = cursor.fetchall()

                    quarter_scores = {1: {'daily': [], 'control': []},
                                      2: {'daily': [], 'control': []},
                                      3: {'daily': [], 'control': []},
                                      4: {'daily': [], 'control': []}}

                    for grade_value, date_str in all_grades:
                        if grade_value is None:
                            continue
                        q = self.get_quarter_from_date(date_str)
                        if q is None:
                            continue
                        if isinstance(grade_value, str) and '/' in grade_value:
                            try:
                                parts = grade_value.split('/')
                                if len(parts) == 2 and all(p.isdigit() for p in parts):
                                    score = sum(int(p) for p in parts) / 2
                                    quarter_scores[q]['control'].append(score)
                            except ValueError:
                                continue
                        elif str(grade_value).isdigit():
                            quarter_scores[q]['daily'].append(int(grade_value))

                    for q in [1, 2, 3, 4]:
                        daily = quarter_scores[q]['daily']
                        control = quarter_scores[q]['control']
                        daily_avg = sum(daily) / len(daily) if daily else None
                        control_avg = sum(control) / len(control) if control else None

                        if daily_avg is not None and control_avg is not None:
                            final = round((daily_avg + control_avg) / 2)
                        elif daily_avg is not None:
                            final = round(daily_avg)
                        elif control_avg is not None:
                            final = round(control_avg)
                        else:
                            final = None

                        if final is not None:
                            quarter_grades_db[q] = final
                            cursor.execute("""
                                INSERT OR REPLACE INTO quarter_grades
                                (student_id, class_name, subject, quarter, grade)
                                VALUES (?, ?, ?, ?, ?)
                            """, (student_id, class_name, subject, q, final))

                    # Аттестатсия
                    cursor.execute("""
                        SELECT att_grade FROM quarter_grades
                        WHERE student_id = ? AND class_name = ? AND subject = ? AND quarter = 0
                    """, (student_id, class_name, subject))
                    att_row = cursor.fetchone()
                    att_final = att_row[0] if att_row and att_row[0] is not None else None

                    q1 = quarter_grades_db.get(1)
                    q2 = quarter_grades_db.get(2)
                    q3 = quarter_grades_db.get(3)
                    q4 = quarter_grades_db.get(4)

                    ns1 = round((q1 + q2) / 2) if q1 is not None and q2 is not None else (q1 if q1 is not None else q2)
                    ns2 = round((q3 + q4) / 2) if q3 is not None and q4 is not None else (q3 if q3 is not None else q4)
                    solona = round((ns1 + ns2) / 2) if ns1 is not None and ns2 is not None else (ns1 if ns1 is not None else ns2)

                    if solona is None and att_final is not None:
                        umumi = att_final
                    elif att_final is not None:
                        umumi = round((solona + att_final) / 2)
                    else:
                        umumi = solona

                    att_from_table = self.table.item(row, 15).text().strip() if self.table.item(row, 15) else ""
                    att_grade_table = int(att_from_table) if att_from_table.isdigit() else att_final

                    final_grades_map = {
                        8: q1,
                        9: q2,
                        10: ns1,
                        11: q3,
                        12: q4,
                        13: ns2,
                        14: solona,
                        15: att_grade_table,
                        16: umumi,
                    }

                    for col_index, grade_val in final_grades_map.items():
                        if col_index < self.table.columnCount():
                            text = str(int(grade_val)) if grade_val is not None else ""
                            item = QTableWidgetItem(text)
                            if col_index == 15:
                                item.setFlags(Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsEditable)
                            else:
                                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                            item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                            self.table.setItem(row, col_index, item)

                conn.commit()
        except sqlite3.Error as e:
            QMessageBox.critical(self, "Хатогӣ", f"Баҳоҳоро ҳисоб карда нашуд: {e}")

    def on_class_or_subject_changed(self):
        """ 
        Синф ёки Фан ўзгарганда, load_class_data'ни фақат self.current_date 
        (яъни охирги танланган сана) асосида юклаш учун чақиради.
        """
        # load_class_data(date_filter=None) ҳолатини аниқ ўрнатамиз
        self.load_class_data(date_filter=None)


    def load_student_photo(self, student_id):
        """ ID бўйича ўқувчи расмини 'data/student_photos' папкасидан юклайди. """
        
        # ID бўлмаса ёки танланмаган бўлса, тозалаш
        if not student_id:
            self.photo_label.setPixmap(QPixmap())
            self.photo_label.setText("Акс вуҷуд надорад")
            self.photo_label.setStyleSheet("border: 1px solid #ccc; background-color: #f0f0f0;")
            return
            
        photo_dir = "data/student_photos"
        
        # 1. Файл номини (.jpg, .jpeg, .png) топиш
        photo_path = None
        for ext in ['.jpg', '.jpeg', '.png']:
            path = os.path.join(photo_dir, f"{student_id}{ext}")
            if os.path.exists(path):
                photo_path = path
                break

        # 2. Расмни юклаш
        if photo_path:
            pixmap = QPixmap(photo_path)
            # Label размерига мослаш (Сизнинг UI'ингиздаги photo_label размерига мослаймиз)
            if not pixmap.isNull():
                # Агар photo_label размери 120x160 деб олсак:
                label_size = QSize(120, 160) 
                scaled_pixmap = pixmap.scaled(label_size, 
                                              Qt.AspectRatioMode.KeepAspectRatio, 
                                              Qt.TransformationMode.SmoothTransformation)
                self.photo_label.setPixmap(scaled_pixmap)
                self.photo_label.setText("") 
                self.photo_label.setStyleSheet("") # Стильни тозалаш
            else:
                self.photo_label.setPixmap(QPixmap())
                self.photo_label.setText("Хатогӣ: Акс бор карда нашуд")
                self.photo_label.setStyleSheet("border: 1px solid red; background-color: #fdd;")
        else:
            # Расм топилмаса
            self.photo_label.setPixmap(QPixmap())
            self.photo_label.setText("Акс вуҷуд надорад")
            self.photo_label.setStyleSheet("border: 1px solid #ccc; background-color: #f0f0f0;")

    def display_student_photo_by_cursor(self, current_item, previous_item):
        """ 
        Курсор (жорий ячейка) ўзгарганда расмни юклайди.
        """
        if current_item is None:
            self.load_student_photo(None)
            return

        # 1-устундаги Ўқувчи коди (ID)ни оламиз (Т/Р 0, ID 1, Ном 2, ...)
        selected_row = current_item.row()
        id_item = self.table.item(selected_row, 1) 
        
        if id_item:
            student_id = id_item.text()
            self.load_student_photo(student_id)
        else:
            self.load_student_photo(None)    


    def upload_photo(self):
        """Акси хонандаро бор кардан ва бо ID сабт кардан."""
        current_item = self.table.currentItem()
        if current_item is None:
            QMessageBox.warning(self, "Диққат", "Аввал аз ҷадвал хонандаро интихоб кунед ё курсорро ба қатори хонанда гузоред.")
            return

        selected_row = current_item.row()
        id_item = self.table.item(selected_row, 1)
        if not id_item:
            QMessageBox.warning(self, "Хатогӣ", "Рамзи ID-и хонанда ёфт нашуд.")
            return

        student_id = id_item.text()
        name_item = self.table.item(selected_row, 2)
        student_name = name_item.text() if name_item else ""

        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "Аксро интихоб кунед",
            "",
            "Аксҳо (*.png *.jpg *.jpeg)"
        )

        if file_path:
            photo_dir = "data/student_photos"
            os.makedirs(photo_dir, exist_ok=True)
            _, extension = os.path.splitext(file_path)

            for ext in ['.jpg', '.jpeg', '.png']:
                old_path = os.path.join(photo_dir, f"{student_id}{ext}")
                if os.path.exists(old_path):
                    os.remove(old_path)

            new_file_name = f"{student_id}{extension.lower()}"
            destination_path = os.path.join(photo_dir, new_file_name)

            try:
                shutil.copy(file_path, destination_path)
                QMessageBox.information(self, "Муваффақият", f"Акси {student_name} муваффақиятли бор карда шуд ({new_file_name}).")
                self.load_student_photo(student_id)
            except Exception as e:
                QMessageBox.critical(self, "Хатогӣ", f"Аксро нусхабардошта хатогӣ рӯй дод: {e}")

    def display_selected_student_photo(self):
        """ Жадвалда қатор танланганда чақирилади ва расмни юклайди. """
        selected_rows = self.table.selectionModel().selectedRows()
        
        if selected_rows:
            # Биринчи танланган қаторни оламиз
            selected_row = selected_rows[0].row()
            
            # 1-устундаги Ўқувчи коди (ID)ни оламиз (Т/Р 0, ID 1, Ном 2, ...)
            id_item = self.table.item(selected_row, 1) 
            
            if id_item:
                student_id = id_item.text()
                self.load_student_photo(student_id)
            else:
                self.load_student_photo(None) # ID бўлмаса, расмни тозалаш
        else:
            # Ҳеч қандай қатор танланмаган бўлса
            self.load_student_photo(None) 

 
    def add_student_row(self, t_r, student_id, name, grade_display, behavior=None, activity=None, attendance_percent="100%", date=None, avg_grade="N/A"):
        row = self.table.rowCount()
        self.table.insertRow(row)

        font = QFont("Arial", 14)

        # Устунҳо: Т/Р (0), Рамзи ID (1), Ному насаб (2), Хол (10) (3), Тарбия (4), Фаъолият (5), Давомат (6), Сана (7), Ч1 (8)

        # 0. Тартиб рақами (Т/Р)
        tr_item = QTableWidgetItem(str(t_r))
        tr_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        tr_item.setFont(font)
        tr_item.setFlags(Qt.ItemFlag.ItemIsEnabled)
        
        # ✔️ 1. ID коди
        id_item = QTableWidgetItem(str(student_id))
        id_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        id_item.setFont(font)
        id_item.setFlags(Qt.ItemFlag.ItemIsEnabled)
        
        # 2. Ном
        name_item = QTableWidgetItem(name)
        name_item.setFont(font)
        name_item.setFlags(Qt.ItemFlag.ItemIsEnabled)

        # 3. Балл / Ғоиб
        grade_item = QTableWidgetItem(grade_display)
        grade_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        grade_item.setFont(font)

        # 4. Тарбия
        behavior_item = QTableWidgetItem(str(behavior) if behavior is not None else "")
        behavior_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        behavior_item.setFont(font)

        # 5. Фаъолият
        activity_item = QTableWidgetItem(str(activity) if activity is not None else "")
        activity_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        activity_item.setFont(font)
        
        # 6. Давомат Фоизи
        attendance_percent_item = QTableWidgetItem(attendance_percent)
        attendance_percent_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        attendance_percent_item.setFont(font)
        attendance_percent_item.setFlags(Qt.ItemFlag.ItemIsEnabled)

        # 7. Сана
        date_item = QTableWidgetItem(date)
        date_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        date_item.setFont(font)
        date_item.setFlags(Qt.ItemFlag.ItemIsEnabled)


        # 8. Баҳои чоряки 1
        avg_grade_item = QTableWidgetItem(avg_grade)
        avg_grade_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        avg_grade_item.setFont(font)
        avg_grade_item.setFlags(Qt.ItemFlag.ItemIsEnabled)

        # Жадвалга жойлаштириш (Барча индекслар 1 тага силжиди)
        self.table.setItem(row, 0, tr_item)        # ✔️ Т/Р 0-устунга
        self.table.setItem(row, 1, id_item)        # ✔️ ID 1-устунга
        self.table.setItem(row, 2, name_item)      # Ном 2-устунга
        self.table.setItem(row, 3, grade_item)     # Балл 3-устунга
        self.table.setItem(row, 4, behavior_item)  # Тарбия 4-устунга
        self.table.setItem(row, 5, activity_item)  # Фаъолият 5-устунга
        self.table.setItem(row, 6, attendance_percent_item) # Давомат 6-устунга
        self.table.setItem(row, 7, date_item)      # Сана 7-устунга
        self.table.setItem(row, 8, avg_grade_item) # Ч1 8-устун
    
    def add_student(self):
        name = self.student_input.text().strip()
        current_class = self.class_combo.currentText()

        if name and current_class:
            try:
                with sqlite3.connect("data/sinflar.db") as conn:
                    cursor = conn.cursor()
                    cursor.execute("""
                        INSERT INTO students (student_name, class_name)
                        VALUES (?, ?)
                    """, (name, current_class))
                    conn.commit()
            except sqlite3.IntegrityError:
                QMessageBox.warning(self, "Хатогӣ", f"'{name}' номи хонанда аллақачон мавҷуд аст.")
                return
            self.load_class_data()
            self.student_input.clear()

    def remove_student(self):
        selected = self.table.currentRow()
        if selected >= 0:
            student_id = self.table.item(selected, 1).text()
            name_to_remove = self.table.item(selected, 2).text()

            reply = QMessageBox.question(self, 'Тасдиқ',
                f"Сиз ҳақиқатдан ҳам '{name_to_remove}'-ро нест кардан мехоҳед? Ҳамаи баҳоҳо ва аксҳои ӯ низ нест мешаванд.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)

            if reply == QMessageBox.StandardButton.Yes:
                try:
                    with sqlite3.connect("data/sinflar.db") as conn:
                        cursor = conn.cursor()
                        cursor.execute("DELETE FROM students WHERE id = ?", (student_id,))
                        cursor.execute("DELETE FROM grades WHERE student_id = ?", (student_id,))
                        cursor.execute("DELETE FROM quarter_grades WHERE student_id = ?", (student_id,))
                        cursor.execute("DELETE FROM quarter_archive WHERE student_name = ?", (name_to_remove,))
                        conn.commit()

                    photo_dir = "data/student_photos"
                    for ext in ['.jpg', '.jpeg', '.png']:
                        p = os.path.join(photo_dir, f"{student_id}{ext}")
                        if os.path.exists(p):
                            os.remove(p)

                    self.load_class_data()
                except Exception as e:
                    QMessageBox.critical(self, "Хатогӣ", f"Хонандаро нест карда нашуд: {e}")

    def add_class(self):
        class_name = self.class_input.text().strip()
        if class_name:
            # Синф номини алоҳида сақламаймиз, фақат мавжудлигини текшириш учун json'дан фойдаланамиз
            if class_name not in self.classes:
                self.classes[class_name] = []
                self.class_combo.addItem(class_name)
                save_json("classes.json", self.classes)
                self.class_input.clear()
    
    def remove_class(self):
        class_name = self.class_combo.currentText()
        if class_name in self.classes:
            reply = QMessageBox.question(self, 'Тасдиқ',
                f"Сиз ҳақиқатдан ҳам синфи '{class_name}'-ро нест кардан мехоҳед? Ҳамаи хонандаҳо, баҳоҳо ва архиви он низ нест мешаванд.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)

            if reply == QMessageBox.StandardButton.Yes:
                try:
                    with sqlite3.connect("data/sinflar.db") as conn:
                        cursor = conn.cursor()
                        cursor.execute("DELETE FROM students WHERE class_name = ?", (class_name,))
                        cursor.execute("DELETE FROM grades WHERE class_name = ?", (class_name,))
                        cursor.execute("DELETE FROM quarter_grades WHERE class_name = ?", (class_name,))
                        cursor.execute("DELETE FROM quarter_archive WHERE class_name = ?", (class_name,))
                        conn.commit()

                    del self.classes[class_name]
                    save_json("classes.json", self.classes)
                    self.class_combo.removeItem(self.class_combo.currentIndex())
                except Exception as e:
                    QMessageBox.critical(self, "Хатогӣ", f"Синфро нест карда нашуд: {e}")

    def add_subject(self):
        subject = self.subject_input.text().strip()
        if subject and subject not in self.subjects:
            self.subjects.append(subject)
            self.subject_combo.addItem(subject)
            save_json("subjects.json", self.subjects)
            self.subject_input.clear()

    def remove_subject(self):
        subject = self.subject_combo.currentText()
        if subject in self.subjects:
            self.subjects.remove(subject)
            save_json("subjects.json", self.subjects)
            self.subject_combo.removeItem(self.subject_combo.currentIndex())


    def check_and_update_db(self):
        """ grades жадвалида student_id устуни мавжудлигини текширади ва қўшади. """
        with sqlite3.connect("data/sinflar.db") as conn:
            cursor = conn.cursor()
            
            # 1. grades жадвалини янгилаш
            try:
                cursor.execute("ALTER TABLE grades ADD COLUMN student_id TEXT")
            except sqlite3.OperationalError as e:
                # Агар устун мавжуд бўлса, хато бермайди
                if "duplicate column name" not in str(e):
                    print(f"Error checking/adding student_id to grades: {e}")
                    
            # 2. quarter_grades жадвалини яратиш (агар мавжуд бўлмаса)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS quarter_grades (
                    student_id TEXT,
                    class_name TEXT,
                    subject TEXT,
                    quarter INTEGER,
                    grade INTEGER,
                    att_grade INTEGER,
                    UNIQUE(student_id, class_name, subject, quarter)
                )
            """)

            conn.commit()


    def save_grades(self):
        """Ҳамаи баҳоҳо барои санаи ҷорӣ сабт карда мешаванд."""
        if not self.class_combo.currentText() or not self.subject_combo.currentText():
            QMessageBox.warning(self, "Диққат", "Синф ёки фан интихоб карда нашудааст.")
            return

        current_class = self.class_combo.currentText()
        current_subject = self.subject_combo.currentText()
        current_date = self.current_date

        try:
            with sqlite3.connect("data/sinflar.db") as conn:
                cursor = conn.cursor()

                for row in range(self.table.rowCount()):
                    id_item = self.table.item(row, 1)
                    if not id_item:
                        continue
                    student_id = id_item.text()
                    name_item = self.table.item(row, 2)
                    student_name = name_item.text() if name_item else ""

                    grade_display = self.table.item(row, 3).text().strip() if self.table.item(row, 3) else ""
                    behavior_text = self.table.item(row, 4).text().strip() if self.table.item(row, 4) else ""
                    activity_text = self.table.item(row, 5).text().strip() if self.table.item(row, 5) else ""
                    att_text = self.table.item(row, 15).text().strip() if self.table.item(row, 15) else ""

                    grade = None
                    attendance = 'б'
                    grade_type = 'daily'

                    if grade_display.lower() == 'ғ':
                        attendance = 'ғ'
                        grade = None
                    elif grade_display:
                        if '/' in grade_display:
                            parts = grade_display.split('/')
                            if len(parts) == 2 and all(p.isdigit() for p in parts):
                                grade = grade_display
                                grade_type = 'control'
                            else:
                                QMessageBox.warning(self, "Хатогӣ", f"{student_name} учун формати баҳо нодуруст аст. '10/8' ё рақамро истифода баред.")
                                return
                        elif grade_display.isdigit():
                            grade = int(grade_display)
                        else:
                            QMessageBox.warning(self, "Хатогӣ", f"{student_name} учун баҳо нодуруст аст. Фақат рақам, '10/8' ёки 'ғ' киритинг.")
                            return

                    behavior = int(behavior_text) if behavior_text.isdigit() else None
                    activity = int(activity_text) if activity_text.isdigit() else None
                    att_grade = int(att_text) if att_text.isdigit() else None

                    cursor.execute("""
                        SELECT id FROM grades
                        WHERE student_id = ? AND class_name = ? AND subject = ? AND date = ?
                    """, (student_id, current_class, current_subject, current_date))
                    existing = cursor.fetchone()

                    has_data = grade is not None or behavior is not None or attendance == 'ғ' or activity is not None

                    if existing:
                        cursor.execute("""
                            UPDATE grades
                            SET student_name = ?, grade = ?, behavior = ?, attendance = ?, activity_grade = ?, grade_type = ?
                            WHERE id = ?
                        """, (student_name, grade, behavior, attendance, activity, grade_type, existing[0]))
                    elif has_data:
                        cursor.execute("""
                            INSERT INTO grades
                            (student_id, student_name, class_name, subject, date, grade, behavior, attendance, activity_grade, grade_type)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """, (student_id, student_name, current_class, current_subject, current_date, grade, behavior, attendance, activity, grade_type))

                    if att_grade is not None:
                        cursor.execute("""
                            INSERT OR REPLACE INTO quarter_grades
                            (student_id, class_name, subject, quarter, att_grade)
                            VALUES (?, ?, ?, 0, ?)
                        """, (student_id, current_class, current_subject, att_grade))
                    else:
                        cursor.execute("""
                            DELETE FROM quarter_grades
                            WHERE student_id = ? AND class_name = ? AND subject = ? AND quarter = 0
                        """, (student_id, current_class, current_subject))

                conn.commit()

            QMessageBox.information(self, "Муваффақият", f"Баҳоҳо барои санаи {current_date} муваффақиятли сабт карда шуданд!")
            self.calculate_all_quarter_grades()
            self.load_class_data(date_filter=current_date)
            self.export_to_excel()
        except sqlite3.Error as e:
            QMessageBox.critical(self, "Хатогӣ", f"Баҳоҳо сабт карда нашуданд: {e}")

    def finish_quarter_and_archive(self):
        # 1. Рақами чоракро пурсида истодааст
        quarter, ok = QInputDialog.getInt(self,
                                          "Чоракни якунлаш",
                                          "Кадом чоракро хотима мехоҳед? (1, 2, 3 ё 4)",
                                          1, 1, 4, 1)
        if not ok:
            return

        # 2. Соли таҳсилӣ
        current_year = datetime.now().strftime("%Y")
        next_year = str(int(current_year) + 1)
        academic_year = f"{current_year}-{next_year}"

        reply = QMessageBox.question(self, 'Тасдиқ',
                                     f"Шумо {quarter}-чоракро хотима мебахшед. Ҳамаи баҳоҳои миёнаи фанҳо ба архив мегузаранд. Идома медиҳед?",
                                     QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                     QMessageBox.StandardButton.No)

        if reply == QMessageBox.StandardButton.No:
            return

        try:
            with sqlite3.connect("data/sinflar.db") as conn:
                cursor = conn.cursor()

                # A. Баҳоҳои миёнаи чоракро аз ҷадвали quarter_grades мегирад
                cursor.execute("""
                    SELECT q.class_name, s.student_name, q.subject, q.grade
                    FROM quarter_grades q
                    JOIN students s ON q.student_id = s.id
                    WHERE q.quarter = ? AND q.grade IS NOT NULL
                """, (quarter,))

                archived = 0
                for class_name, student_name, subject, grade in cursor.fetchall():
                    cursor.execute("""
                        INSERT OR REPLACE INTO quarter_archive
                        (student_name, class_name, subject, quarter, year, avg_grade)
                        VALUES (?, ?, ?, ?, ?, ?)
                    """, (student_name, class_name, subject, quarter, academic_year, grade))
                    archived += 1

                # B. Баҳоиҳои кундалӣ нест карда мешаванд
                cursor.execute("DELETE FROM grades")

                conn.commit()
                QMessageBox.information(self, "Муваффақият", f"{quarter}-чорак бомуваффақият ба архив гузаронида шуд. Сабтҳои кундалӣ тоза карда шуданд.")
                self.load_class_data()

        except sqlite3.Error as e:
            QMessageBox.critical(self, "Хатогӣ", f"Чоракро хотима додан мумкин нест: {e}")
            return

        # Пас аз боркунии дубора баҳоҳоро ҳисоб мекунад
        self.calculate_all_quarter_grades() 

    def show_report_window(self):
        current_class = self.class_combo.currentText()
        current_subject = self.subject_combo.currentText()
        
        if not current_class or not current_subject:
            QMessageBox.warning(self, "Диққат", "Аввал синф ва фанни танланг.")
            return

        # Ҳисобот ойнаси ҳали яратилмаган бўлса, яратамиз
        if self.report_window is None:
            self.report_window = ReportWindow(db_path="data/sinflar.db")
        
        # 1. Ҳисобот ойнасини жорий синф ва фан маълумотлари билан янгилаймиз
        self.report_window.load_report_data(current_class, current_subject) 
        
        # 2. Ойнани кўрсатамиз (агар яширилган бўлса) ва уни фокусга оламиз
        self.report_window.show()
        self.report_window.activateWindow()
        self.report_window.raise_()


    def show_date_selector(self):
        """Санаи гузашта ё ояндаро интихоб кардан барои воридкунии/таҳрири баҳоҳо."""
        dialog = QDialog(self)
        dialog.setWindowTitle("Интихоби сана")
        dialog.setFixedSize(350, 400)

        layout = QVBoxLayout(dialog)

        calendar = QCalendarWidget()
        calendar.setFont(QFont("Arial", 11))
        try:
            d, m, y = map(int, self.current_date.split('.'))
            calendar.setSelectedDate(QDate(y, m, d))
        except:
            calendar.setSelectedDate(QDate.currentDate())

        layout.addWidget(calendar)

        ok_button = QPushButton("Баҳоҳои интихобшударо нишон додан")
        ok_button.setFont(QFont("Arial", 12))
        layout.addWidget(ok_button)

        def on_ok_clicked():
            selected_date_str = calendar.selectedDate().toString("dd.MM.yyyy")
            self.current_date = selected_date_str
            if hasattr(self, 'active_date_label'):
                self.active_date_label.setText(f"Санаи фаъол: {self.current_date}")
            self.load_class_data(date_filter=selected_date_str)
            self.edit_past_grades_btn.setText(f"Баҳои санаи: {selected_date_str}")
            dialog.accept()
            QMessageBox.information(self, "Диққат", f"{selected_date_str} сана интихоб шуд. Акнун баҳоҳоро тағйир дода, 'Сабт кардан'-ро пахш кунед.")

        ok_button.clicked.connect(on_ok_clicked)
        dialog.exec()


    def get_quarter_from_date(self, date_str):
        """Санаи dd.MM.YYYY-ро ба чораки таҳсилӣ (1-4) мегардонад."""
        try:
            d, m, y = map(int, date_str.split('.'))
            if 9 <= m <= 11:
                return 1
            elif m == 12 or m <= 2:
                return 2
            elif 3 <= m <= 5:
                return 3
            else:
                return 4
        except Exception:
            return 1

    def _get_umumi_grade(self, student_id, class_name, subject):
        """Баҳои умумиро аз ҷадвали quarter_grades ҳисоб мекунад."""
        try:
            with sqlite3.connect("data/sinflar.db") as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT quarter, grade FROM quarter_grades
                    WHERE student_id = ? AND class_name = ? AND subject = ? AND quarter IN (1,2,3,4)
                """, (student_id, class_name, subject))
                q_map = {q: g for q, g in cursor.fetchall()}

                cursor.execute("""
                    SELECT att_grade FROM quarter_grades
                    WHERE student_id = ? AND class_name = ? AND subject = ? AND quarter = 0
                """, (student_id, class_name, subject))
                att_row = cursor.fetchone()
                att = att_row[0] if att_row and att_row[0] is not None else None
        except sqlite3.Error:
            return None

        q1 = q_map.get(1)
        q2 = q_map.get(2)
        q3 = q_map.get(3)
        q4 = q_map.get(4)

        ns1 = round((q1 + q2) / 2) if q1 is not None and q2 is not None else (q1 if q1 is not None else q2)
        ns2 = round((q3 + q4) / 2) if q3 is not None and q4 is not None else (q3 if q3 is not None else q4)
        solona = round((ns1 + ns2) / 2) if ns1 is not None and ns2 is not None else (ns1 if ns1 is not None else ns2)

        if solona is None and att is not None:
            return att
        if att is not None:
            return round((solona + att) / 2)
        return solona

    def export_to_excel(self):
        """Баҳои умумии синф/фанро ба Excel-и манзили муштарак сабт мекунад."""
        current_class = self.class_combo.currentText()
        current_subject = self.subject_combo.currentText()
        if not current_class or not current_subject:
            return

        source_dir = "data/source_files"
        os.makedirs(source_dir, exist_ok=True)

        def to_latin(name):
            cyr = {
                'а':'a','б':'b','в':'v','г':'g','д':'d','е':'e','ё':'yo','ж':'j','з':'z','и':'i','й':'y','к':'k','л':'l','м':'m','н':'n','о':'o','п':'p','р':'r','с':'s','т':'t','у':'u','ф':'f','х':'h','ц':'ts','ч':'ch','ш':'sh','щ':'sh','ъ':'','ы':'i','ь':'','э':'e','ю':'yu','я':'ya',
                'А':'A','Б':'B','В':'V','Г':'G','Д':'D','Е':'E','Ё':'Yo','Ж':'J','З':'Z','И':'I','Й':'Y','К':'K','Л':'L','М':'M','Н':'N','О':'O','П':'P','Р':'R','С':'S','Т':'T','У':'U','Ф':'F','Х':'H','Ц':'Ts','Ч':'Ch','Ш':'Sh','Щ':'Sh','Ъ':'','Ы':'I','Ь':'','Э':'E','Ю':'Yu','Я':'Ya',
                'ҷ':'j','ҳ':'h','қ':'q','ғ':'g','ӣ':'i','ӯ':'u','Ҷ':'J','Ҳ':'H','Қ':'Q','Ғ':'G','Ӣ':'I','Ӯ':'U'
            }
            s = ''.join(cyr.get(ch, ch) for ch in name)
            return ''.join(ch for ch in s if ch.isalnum()).lower()

        filename = to_latin(current_class) + ".xlsx"
        file_path = os.path.join(source_dir, filename)

        try:
            if os.path.exists(file_path):
                wb = load_workbook(file_path)
                file_existed = True
            else:
                wb = Workbook()
                if 'Sheet' in wb.sheetnames:
                    wb.remove(wb['Sheet'])
                file_existed = False

            sheet_name = current_class[:31]
            if sheet_name not in wb.sheetnames:
                ws = wb.create_sheet(title=sheet_name)
                ws.append(["Синф", "Ном ва насаб"] + self.subjects)
                thin = Side(style="thin", color="000000")
                header_fill = PatternFill(start_color="D9E1F2", end_color="D9E1F2", fill_type="solid")
                for cell in ws[1]:
                    cell.font = Font(bold=True)
                    cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
                    cell.fill = header_fill
                    cell.border = Border(left=thin, right=thin, top=thin, bottom=thin)
                ws.freeze_panes = "A2"
            else:
                ws = wb[sheet_name]

            thin = Side(style="thin", color="000000")
            for col_idx, subj in enumerate(self.subjects, start=3):
                cell = ws.cell(row=1, column=col_idx)
                if cell.value != subj:
                    cell.value = subj
                    cell.font = Font(bold=True)
                    cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
                    cell.border = Border(left=thin, right=thin, top=thin, bottom=thin)

            active_col = None
            for col_idx, subj in enumerate(self.subjects, start=3):
                if subj == current_subject:
                    active_col = col_idx
                    break
            if active_col is None:
                QMessageBox.warning(self, "Хатогӣ", "Фани интихобшуда дар рӯйхати фанҳо ёфт нашуд.")
                return

            with sqlite3.connect("data/sinflar.db") as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT id, student_name FROM students
                    WHERE class_name = ?
                    ORDER BY student_name
                """, (current_class,))
                students = cursor.fetchall()

            for student_id, student_name in students:
                row_idx = None
                for r in range(2, ws.max_row + 1):
                    if ws.cell(row=r, column=2).value == student_name:
                        row_idx = r
                        break
                if row_idx is None:
                    ws.append([current_class, student_name])
                    row_idx = ws.max_row

                ws.cell(row=row_idx, column=1).value = current_class
                ws.cell(row=row_idx, column=2).value = student_name

                if file_existed:
                    val = self._get_umumi_grade(student_id, current_class, current_subject)
                    ws.cell(row=row_idx, column=active_col).value = val if val is not None else ""
                else:
                    for col_idx, subj in enumerate(self.subjects, start=3):
                        val = self._get_umumi_grade(student_id, current_class, subj)
                        ws.cell(row=row_idx, column=col_idx).value = val if val is not None else ""

                for cell in ws[row_idx]:
                    cell.border = Border(left=thin, right=thin, top=thin, bottom=thin)
                    cell.alignment = Alignment(horizontal="center", vertical="center")

            wb.save(file_path)
            QMessageBox.information(self, "Муваффақият", f"Файли Excel навсозӣ шуд:\n{file_path}")
        except PermissionError:
            QMessageBox.warning(self, "Диққат", f"Файли {file_path} аз тарафи истифодабарандаи дигар кушода шудааст. Илтимос, онро пӯшед ва бори дигар кӯшиш кунед.")
        except Exception as e:
            QMessageBox.critical(self, "Хатогӣ", f"Excel сабт карда нашуд: {e}")


# 🔥 Бошлаш
if __name__ == "__main__":
    create_grades_table()
    app = QApplication(sys.argv)
    window = StudentApp()
    window.show()
    sys.exit(app.exec())