"""GemmaVideo desktop interface: source library and semantic video search."""
from pathlib import Path
import argparse
import json
import os
import sys
import threading

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
os.environ.setdefault('OPENBLAS_NUM_THREADS', '4')

import numpy as np
from PySide6.QtCore import Qt, QThread, Signal, QTimer, QUrl, QLockFile, QPointF, QRectF, QCoreApplication, QLibraryInfo
QT_PLUGINS = QLibraryInfo.path(QLibraryInfo.LibraryPath.PluginsPath)
os.environ['QT_PLUGIN_PATH'] = QT_PLUGINS
os.environ['QT_QPA_PLATFORM_PLUGIN_PATH'] = str(Path(QT_PLUGINS) / 'platforms')
QCoreApplication.setLibraryPaths([QT_PLUGINS])
from PySide6.QtGui import QColor, QFontDatabase, QPainter, QPen, QImage
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QTabWidget, QFileDialog, QMessageBox, QLineEdit, QComboBox, QSplitter, QTableWidget,
    QTableWidgetItem, QHeaderView, QAbstractItemView, QProgressBar, QListWidget,
    QListWidgetItem, QSlider, QFrame, QDoubleSpinBox,
)
from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput, QVideoSink
from video_gemma.common import APP, ROOT, DEFAULT_SOURCE, Cancelled, duration
from video_gemma.library import Library
from video_gemma.media import SUPPORTED, fingerprint
from video_gemma.model import Engine
from video_gemma.theme import STYLE


class Task(QThread):
    progress = Signal(object)
    result = Signal(object)
    failure = Signal(str)

    def __init__(self, fn, parent=None):
        super().__init__(parent)
        self.fn = fn
        self.stop = threading.Event()

    def run(self):
        try:
            self.result.emit(self.fn(self.stop, self.progress.emit))
        except Cancelled as e:
            self.failure.emit(str(e))
        except Exception as e:
            self.failure.emit(str(e))


class VideoCanvas(QWidget):
    """Qt's timed video sink, painted into the same widget tree as the controls.

    Works with software decoding too, without depending on a GL window/overlay.
    QMediaPlayer remains responsible for A/V synchronization and seeking.
    """
    def __init__(self, parent=None):
        super().__init__(parent)
        self.sink = QVideoSink(self)
        self.frame = QImage()
        self.sink.videoFrameChanged.connect(self.present)
        self.setMinimumSize(400, 225)

    def videoSink(self):
        return self.sink

    def present(self, frame):
        self.frame = frame.toImage() if frame.isValid() else QImage()
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor('#20292e'))
        if self.frame.isNull():
            painter.setPen(QColor('#bbc7c5'))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, 'Il video apparirà qui')
        else:
            size = self.frame.size().scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatio)
            rect = QRectF((self.width() - size.width()) / 2, (self.height() - size.height()) / 2,
                          size.width(), size.height())
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            painter.drawImage(rect, self.frame)


class Waveform(QWidget):
    seek = Signal(float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(120)
        self.setMaximumHeight(190)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip('Clicca o trascina sulla forma d’onda per spostarti nel video. Le fasce azzurre indicano i risultati.')
        self.duration = 0.
        self.position = 0.
        self.peaks = np.empty((0, 2))
        self.intervals = []
        self.message = 'Seleziona un risultato per vedere la traccia audio.'
        self.setObjectName('waveform')

    def set_media(self, seconds, peaks, intervals, message=''):
        self.duration = seconds
        self.position = 0.
        self.peaks = peaks
        self.intervals = intervals
        self.message = message
        self.update()

    def set_position(self, position):
        self.position = position
        self.update()

    def plot_rect(self):
        return QRectF(12, 20, max(1, self.width() - 24), max(1, self.height() - 49))

    def paintEvent(self, event):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor('#f6f3e9'))
        rect = self.plot_rect()
        p.setPen(QPen(QColor('#c1c2b7'), 1))
        p.drawRect(rect)
        if not self.duration:
            p.setPen(QColor('#66706e'))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self.message)
            return
        for interval in self.intervals:
            x = rect.left() + interval['start'] / self.duration * rect.width()
            width = max(2., (interval['end'] - interval['start']) / self.duration * rect.width())
            p.fillRect(QRectF(x, rect.top(), width, rect.height()), QColor(83, 137, 146, 75))
            p.setPen(QPen(QColor('#538992'), 1))
            p.drawLine(QPointF(x, rect.top()), QPointF(x, rect.bottom()))
        center = rect.center().y()
        p.setPen(QPen(QColor('#c1c2b7'), 1))
        p.drawLine(QPointF(rect.left(), center), QPointF(rect.right(), center))
        if len(self.peaks):
            # Aggregate extrema to each screen pixel; spikes survive zooming out.
            width = max(1, int(rect.width()))
            bounds = np.linspace(0, len(self.peaks), min(width, len(self.peaks)) + 1, dtype=int)
            gain = .46 * rect.height() / max(.02, float(np.max(np.abs(self.peaks))))
            p.setPen(QPen(QColor('#477983'), 1))
            for i in range(len(bounds) - 1):
                values = self.peaks[bounds[i]:bounds[i + 1]]
                if len(values):
                    x = rect.left() + i * rect.width() / (len(bounds) - 1)
                    p.drawLine(QPointF(x, center - values[:, 1].max() * gain),
                               QPointF(x, center - values[:, 0].min() * gain))
        elif self.message:
            p.setPen(QColor('#66706e'))
            p.drawText(rect, Qt.AlignmentFlag.AlignCenter, self.message)
        p.setPen(QPen(QColor('#b18059'), 2))
        x = rect.left() + min(1., self.position / self.duration) * rect.width()
        p.drawLine(QPointF(x, rect.top() - 7), QPointF(x, rect.bottom() + 3))
        p.setPen(QColor('#66706e'))
        for i in range(5):
            text = duration(self.duration * i / 4)
            x = rect.left() + rect.width() * i / 4
            text_width = p.fontMetrics().horizontalAdvance(text)
            x = max(rect.left(), min(x - text_width / 2, rect.right() - text_width))
            p.drawText(QPointF(x, self.height() - 7), text)
        p.drawText(QPointF(12, 14), 'AUDIO · intervalli trovati in azzurro')

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.seek_at(event.position().x())

    def mouseMoveEvent(self, event):
        if event.buttons() & Qt.MouseButton.LeftButton:
            self.seek_at(event.position().x())

    def seek_at(self, x):
        if self.duration:
            rect = self.plot_rect()
            self.seek.emit(max(0., min(1., (x - rect.left()) / rect.width())) * self.duration)


def label(text, name=None):
    widget = QLabel(text)
    if name:
        widget.setObjectName(name)
    return widget


def button(text, slot, primary=False):
    widget = QPushButton(text)
    if primary:
        widget.setObjectName('primary')
    widget.clicked.connect(slot)
    return widget


class Window(QMainWindow):
    def __init__(self, data=APP / 'dati', source=None, backend=None, audio_enabled=True):
        super().__init__()
        self.library = Library(data)
        self.source = str(source or self.library.last_source() or DEFAULT_SOURCE)
        self.visible_videos = []
        self.refreshing = False
        self.setAcceptDrops(True)
        self.worker = None
        self.engine = None
        self.results = []
        self.current_result = None
        self.pending_seek = None
        self.closing = False
        self.last_errors = []
        self.setWindowTitle('GemmaVideo · cerca dentro i tuoi video')
        self.resize(1360, 930)
        self.setMinimumSize(1040, 740)
        central = QWidget()
        self.setCentralWidget(central)
        outer = QVBoxLayout(central)
        outer.setContentsMargins(24, 18, 24, 16)
        header = QHBoxLayout()
        title = QVBoxLayout()
        title.addWidget(label('GEMMA VIDEO', 'brand'))
        title.addWidget(label('Una descrizione. I momenti da ritrovare.', 'muted'))
        header.addLayout(title)
        header.addStretch()
        header.addWidget(label('Motore locale'))
        self.backend = QComboBox()
        self.backend.addItem('CPU', 'cpu')
        self.backend.addItem('GPU · Vulkan', 'vulkan')
        selected = backend or ('vulkan' if list(Path('/dev/dri').glob('renderD*')) else 'cpu')
        self.backend.setCurrentIndex(self.backend.findData(selected))
        header.addWidget(self.backend)
        outer.addLayout(header)
        self.tabs = QTabWidget()
        outer.addWidget(self.tabs, 1)
        self.build_library()
        self.build_search()
        footer = QHBoxLayout()
        self.status = label('Pronto.', 'muted')
        self.status.setWordWrap(True)
        footer.addWidget(self.status, 1)
        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.setMaximumWidth(210)
        self.progress.setFormat('%p%')
        footer.addWidget(self.progress)
        self.stop_button = button('Sospendi', self.cancel)
        self.stop_button.setEnabled(False)
        footer.addWidget(self.stop_button)
        self.details_button = button('Dettagli', self.show_errors)
        self.details_button.setEnabled(False)
        footer.addWidget(self.details_button)
        outer.addLayout(footer)
        self.player = QMediaPlayer(self)
        self.audio = QAudioOutput(self) if audio_enabled else None
        if self.audio:
            self.audio.setVolume(.7)
            self.player.setAudioOutput(self.audio)
        else:
            self.volume.setEnabled(False)
            self.volume.setToolTip('Uscita audio disabilitata per questa sessione.')
        self.player.setVideoSink(self.video.videoSink())
        self.player.positionChanged.connect(self.position_changed)
        self.player.durationChanged.connect(lambda ms: self.slider.setRange(0, ms))
        self.player.playbackStateChanged.connect(self.play_state)
        self.player.mediaStatusChanged.connect(self.media_status)
        self.player.errorOccurred.connect(self.playback_error)
        self.slider.sliderMoved.connect(self.player.setPosition)
        self.slider.sliderReleased.connect(lambda: self.player.setPosition(self.slider.value()))
        self.waveform.seek.connect(lambda seconds: self.player.setPosition(round(seconds * 1000)))
        self.volume.valueChanged.connect(lambda value: self.audio and self.audio.setVolume(value / 100))
        self.refresh()
        if not self.library.videos():
            self.status.setText('Archivio vuoto. Premi «Aggiungi video…» o «Aggiungi cartella…», oppure trascina qui i file.')

    def build_library(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(20, 20, 20, 18)
        layout.addWidget(label('I TUOI VIDEO', 'section'))
        hint = label('1. Aggiungi video o una cartella   →   2. Analizza i video   →   3. Cerca nella scheda Ricerca. '
                     'Puoi anche trascinare qui file e cartelle.', 'muted')
        hint.setWordWrap(True)
        layout.addWidget(hint)
        actions = QHBoxLayout()
        self.add_files_button = button('Aggiungi video…', self.choose_files, True)
        self.browse_button = button('Aggiungi cartella…', self.choose_folder)
        self.remove_button = button('Rimuovi selezionati…', self.confirm_remove)
        self.remove_button.setToolTip('Rimuove le voci dall’archivio e i loro embedding. Conserva i file originali.')
        actions.addWidget(self.add_files_button)
        actions.addWidget(self.browse_button)
        actions.addStretch()
        actions.addWidget(self.remove_button)
        layout.addLayout(actions)
        row = QHBoxLayout()
        self.source_edit = QLineEdit(self.source)
        self.source_edit.setAcceptDrops(False)
        self.source_edit.setPlaceholderText('Percorso di una cartella o di un video')
        self.source_edit.returnPressed.connect(self.scan)
        self.scan_button = button('Aggiungi percorso', self.scan)
        row.addWidget(self.source_edit, 1)
        row.addWidget(self.scan_button)
        layout.addLayout(row)
        self.import_summary = label('Aggiungere rende i video visibili nell’archivio. «Analizza» li rende ricercabili.', 'muted')
        self.import_summary.setWordWrap(True)
        layout.addWidget(self.import_summary)
        filters = QHBoxLayout()
        filters.addWidget(label('Mostra'))
        self.library_filter = QComboBox()
        self.library_filter.addItem('Tutti i video', 'all')
        self.library_filter.addItem('File disponibili', 'available')
        self.library_filter.addItem('File mancanti / spostati', 'missing')
        self.library_filter.addItem('Da analizzare o riprendere', 'pending')
        self.library_filter.currentIndexChanged.connect(self.refresh)
        filters.addWidget(self.library_filter)
        self.selected_label = label('Nessun video selezionato', 'muted')
        filters.addWidget(self.selected_label)
        filters.addStretch()
        self.summary = label('', 'muted')
        filters.addWidget(self.summary)
        layout.addLayout(filters)
        row = QHBoxLayout()
        row.addWidget(label('Un fotogramma ogni'))
        self.step = QComboBox()
        for value in (1., 2., 4., 8.):
            self.step.addItem(f'{value:g} secondi', value)
        self.step.setCurrentIndex(2)
        self.step.setToolTip('1–2 s per eventi brevi; 4 s per lezioni e registrazioni dello schermo. '
                             'Il passo scelto vale per i video non ancora iniziati.')
        row.addWidget(self.step)
        row.addWidget(label('Audio: finestre di 20 s, sovrapposizione di 4 s', 'muted'))
        row.addStretch()
        layout.addLayout(row)
        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(['Video', 'Durata', 'Analizzato', 'Embedding', 'Passo', 'Stato', 'File di origine'])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        for col in range(1, 6):
            self.table.horizontalHeader().setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(6, QHeaderView.ResizeMode.Stretch)
        self.table.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.table.verticalHeader().hide()
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.itemDoubleClicked.connect(self.preview_selected)
        self.table.itemSelectionChanged.connect(self.update_selection)
        layout.addWidget(self.table, 1)
        row = QHBoxLayout()
        self.index_button = button('Analizza / riprendi tutti', lambda: self.index(False), True)
        self.selected_button = button('Analizza selezionati', lambda: self.index(True))
        self.reset_button = button('Elimina embedding', self.confirm_reset)
        row.addWidget(self.index_button)
        row.addWidget(self.selected_button)
        row.addStretch()
        row.addWidget(self.reset_button)
        layout.addLayout(row)
        note = label('Ctrl o Maiusc per selezionare più video; Ctrl+A seleziona tutte le righe visibili. '
                     '«Rimuovi selezionati» conserva i file originali. Durante l’analisi premi Sospendi per gestire l’archivio.', 'muted')
        note.setWordWrap(True)
        layout.addWidget(note)
        self.tabs.addTab(page, 'Indicizzazione')

    def build_search(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(20, 20, 20, 18)
        heading = label('CHE COSA VUOI RITROVARE?', 'section')
        heading.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(heading)
        search_row = QHBoxLayout()
        search_row.addStretch(1)
        self.query = QLineEdit()
        self.query.setMinimumWidth(440)
        self.query.setMaximumWidth(820)
        self.query.setMaxLength(6000)
        self.query.setPlaceholderText('Descrivi un’idea, una scena o qualcosa che viene spiegato…')
        self.query.setClearButtonEnabled(True)
        self.query.returnPressed.connect(self.search)
        self.search_button = button('Cerca nei video', self.search, True)
        search_row.addWidget(self.query, 6)
        search_row.addWidget(self.search_button)
        search_row.addStretch(1)
        layout.addLayout(search_row)
        options = QHBoxLayout()
        options.addStretch()
        options.addWidget(label('Cerca in'))
        self.mode = QComboBox()
        self.mode.addItem('Audio e immagini', 'both')
        self.mode.addItem('Solo audio', 'audio')
        self.mode.addItem('Solo immagini', 'frame')
        options.addWidget(self.mode)
        options.addWidget(label('Somiglianza minima'))
        self.minimum = QDoubleSpinBox()
        self.minimum.setRange(0, 1)
        self.minimum.setSingleStep(.02)
        self.minimum.setDecimals(2)
        self.minimum.setValue(.65)
        self.minimum.setToolTip('Punteggio di somiglianza, non probabilità. Alza la soglia per limitare i risultati generici.')
        options.addWidget(self.minimum)
        options.addStretch()
        layout.addLayout(options)
        self.search_summary = label('L’indice è locale. Cerca anche con parole diverse da quelle pronunciate.', 'muted')
        self.search_summary.setWordWrap(True)
        layout.addWidget(self.search_summary)
        split = QSplitter(Qt.Orientation.Horizontal)
        self.result_list = QListWidget()
        self.result_list.setMinimumWidth(250)
        self.result_list.setWordWrap(True)
        self.result_list.currentRowChanged.connect(self.select_result)
        self.result_list.itemClicked.connect(self.retry_result)
        split.addWidget(self.result_list)
        viewer = QFrame()
        viewer.setObjectName('panel')
        vbox = QVBoxLayout(viewer)
        vbox.setContentsMargins(12, 12, 12, 12)
        self.video_title = label('Seleziona un risultato', 'section')
        self.video_title.setWordWrap(True)
        vbox.addWidget(self.video_title)
        self.video = VideoCanvas()
        vbox.addWidget(self.video, 1)
        self.waveform = Waveform()
        vbox.addWidget(self.waveform)
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(0, 0)
        self.slider.setToolTip('Spostati lungo l’intera durata del video')
        vbox.addWidget(self.slider)
        controls = QHBoxLayout()
        self.play_button = button('Riproduci', self.toggle_play, True)
        self.play_button.setMinimumWidth(125)
        self.play_button.setEnabled(False)
        controls.addWidget(self.play_button)
        self.time_label = label('00:00:00 / 00:00:00', 'muted')
        controls.addWidget(self.time_label)
        controls.addStretch()
        controls.addWidget(label('Volume'))
        self.volume = QSlider(Qt.Orientation.Horizontal)
        self.volume.setRange(0, 100)
        self.volume.setValue(70)
        self.volume.setMaximumWidth(100)
        controls.addWidget(self.volume)
        vbox.addLayout(controls)
        row = QHBoxLayout()
        row.addWidget(label('Intervallo'))
        self.intervals = QComboBox()
        self.intervals.setMinimumContentsLength(25)
        self.intervals.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.intervals.activated.connect(self.jump_interval)
        row.addWidget(self.intervals, 1)
        self.previous = button('‹', lambda: self.change_interval(-1))
        self.next = button('›', lambda: self.change_interval(1))
        row.addWidget(self.previous)
        row.addWidget(self.next)
        vbox.addLayout(row)
        self.playback_note = label('Gli intervalli sono stime basate su audio e fotogrammi campionati.', 'muted')
        self.playback_note.setWordWrap(True)
        vbox.addWidget(self.playback_note)
        split.addWidget(viewer)
        split.setStretchFactor(0, 0)
        split.setStretchFactor(1, 1)
        split.setSizes([300, 900])
        layout.addWidget(split, 1)
        self.tabs.addTab(page, 'Ricerca')

    def refresh(self):
        videos = self.library.videos()
        selected = set(self.selected_ids())
        mode = self.library_filter.currentData()
        for video in videos:
            video['available'] = Path(video['path']).is_file()
        total = len(videos)
        missing = sum(not v['available'] for v in videos)
        ready = sum(v['status'] == 'pronto' and v['available'] for v in videos)
        if mode == 'missing':
            videos = [v for v in videos if not v['available']]
        elif mode == 'available':
            videos = [v for v in videos if v['available']]
        elif mode == 'pending':
            videos = [v for v in videos if v['available'] and v['checkpoint'] < v['duration'] - 1e-6]
        self.visible_videos = videos
        self.refreshing = True
        self.table.clearSelection()
        self.table.setRowCount(len(videos))
        for row, video in enumerate(videos):
            percent = video['checkpoint'] / video['duration'] * 100
            state = video['status']
            if state == 'analisi' and not self.worker:
                state = 'da riprendere'
            if not video['available']:
                state = 'File mancante'
            values = [video['name'], duration(video['duration']), f'{percent:.1f}%',
                      str(video['vectors']), f'{video["frame_step"]:g} s' if video['checkpoint'] else '—', state, video['path']]
            for col, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setData(Qt.ItemDataRole.UserRole, video['id'])
                item.setToolTip(video['path'] + ('\n' + video['error'] if video['error'] else ''))
                if not video['available']:
                    item.setForeground(QColor('#a56f50'))
                self.table.setItem(row, col, item)
            if video['id'] in selected:
                for col in range(self.table.columnCount()):
                    self.table.item(row, col).setSelected(True)
        self.refreshing = False
        self.summary.setText(f'{len(videos)}/{total} video · {ready} pronti · {missing} mancanti')
        self.update_selection()

    def selected_ids(self):
        return [self.table.item(i.row(), 0).data(Qt.ItemDataRole.UserRole)
                for i in self.table.selectionModel().selectedRows() if self.table.item(i.row(), 0)]

    def update_selection(self):
        if self.refreshing:
            return
        count = len(self.selected_ids())
        self.selected_label.setText(f'{count} video selezionati' if count else 'Nessun video selezionato')
        self.remove_button.setEnabled(bool(count) and self.worker is None)
        ids = set(self.selected_ids())
        available = any(v['id'] in ids and v['available'] for v in self.visible_videos)
        self.selected_button.setEnabled(available and self.worker is None)

    def set_busy(self, value):
        for widget in (self.add_files_button, self.browse_button, self.scan_button, self.index_button, self.selected_button, self.remove_button,
                       self.reset_button, self.backend, self.step, self.source_edit, self.search_button):
            widget.setEnabled(not value)
        self.stop_button.setEnabled(value)
        if not value:
            self.update_selection()

    def start_task(self, fn, done=None):
        if self.worker:
            return
        self.last_errors = []
        self.details_button.setEnabled(False)
        self.progress.setRange(0, 0)
        self.set_busy(True)
        self.worker = Task(fn, self)
        self.worker.progress.connect(self.task_progress)
        self.worker.result.connect(done or self.task_result)
        self.worker.failure.connect(self.task_failed)
        self.worker.finished.connect(self.task_finished)
        self.worker.start()

    def task_progress(self, value):
        self.status.setText(value.get('message', ''))
        if 'fraction' in value:
            self.progress.setRange(0, 1000)
            self.progress.setValue(round(value['fraction'] * 1000))
            self.status.setText(self.status.text() + ' · tempo residuo stimato ' + duration(value['eta']))
            self.refresh()

    def task_result(self, value):
        self.status.setText(value.get('message', 'Operazione completata.'))
        self.last_errors = value.get('errors', [])
        self.details_button.setEnabled(bool(self.last_errors))
        self.progress.setRange(0, 1000)
        self.progress.setValue(1000)
        self.refresh()

    def task_failed(self, message):
        self.status.setText(message)
        self.last_errors = [message]
        self.details_button.setEnabled(True)
        self.progress.setRange(0, 1000)
        self.refresh()

    def task_finished(self):
        worker, self.worker = self.worker, None
        worker.deleteLater()
        self.set_busy(False)
        self.progress.setRange(0, 1000)
        self.refresh()
        if self.closing:
            self.close()

    def cancel(self):
        if self.worker:
            self.worker.stop.set()
            self.stop_button.setEnabled(False)
            self.status.setText('Sospensione in corso; gli intervalli completati restano salvati…')

    def get_engine(self):
        backend = self.backend.currentData()
        if self.engine and self.engine.backend != backend:
            self.engine.close()
            self.engine = None
        if self.engine is None:
            self.engine = Engine(self.library.data, backend)
        return self.engine

    def choose_folder(self):
        if self.worker:
            return
        path = QFileDialog.getExistingDirectory(self, 'Aggiungi tutti i video di una cartella', self.source_edit.text(),
                                                QFileDialog.Option.ShowDirsOnly | QFileDialog.Option.DontUseNativeDialog)
        if path:
            self.source_edit.setText(path)
            self.scan()

    def scan(self):
        source = self.source_edit.text().strip()
        self.import_paths([source])

    def choose_files(self):
        if self.worker:
            return
        patterns = ' '.join('*' + suffix for suffix in sorted(SUPPORTED | {suffix.upper() for suffix in SUPPORTED}))
        paths, _ = QFileDialog.getOpenFileNames(self, 'Aggiungi uno o più video', self.source_edit.text(),
                                                f'Video ({patterns});;Tutti i file (*)',
                                                options=QFileDialog.Option.DontUseNativeDialog)
        if paths:
            self.import_paths(paths)

    def import_paths(self, paths):
        if self.worker:
            self.status.setText('Premi Sospendi prima di aggiungere o rimuovere video.')
            return
        self.start_task(lambda stop, notify: self.library.add_paths(paths, stop, notify), self.import_result)

    def import_result(self, result):
        self.task_result(result)
        self.import_summary.setText(result['message'])
        if result.get('source'):
            self.source_edit.setText(result['source'])
        if result.get('video_ids'):
            self.library_filter.setCurrentIndex(0)
            self.refresh()
            selected = set(result['video_ids'])
            self.table.clearSelection()
            for row in range(self.table.rowCount()):
                if self.table.item(row, 0).data(Qt.ItemDataRole.UserRole) in selected:
                    for col in range(self.table.columnCount()):
                        self.table.item(row, col).setSelected(True)
            self.results = []
            self.result_list.clear()
            self.clear_player()
            self.search_summary.setText('Archivio aggiornato. Avvia una ricerca per vedere i risultati attuali.')

    def dragEnterEvent(self, event):
        if not self.worker and event.mimeData().hasUrls() and all(url.isLocalFile() for url in event.mimeData().urls()):
            event.acceptProposedAction()

    def dropEvent(self, event):
        paths = [url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile()]
        if paths and not self.worker:
            event.acceptProposedAction()
            self.import_paths(paths)

    def confirm_remove(self):
        if self.worker:
            return
        ids = self.selected_ids()
        if not ids:
            self.status.setText('Seleziona i video da rimuovere dalla tabella.')
            return
        selected = [v for v in self.library.videos() if v['id'] in ids]
        box = QMessageBox(self)
        box.setWindowTitle('Rimuovere i video dall’archivio?')
        box.setIcon(QMessageBox.Icon.Warning)
        box.setText(f'Rimuovere {len(selected)} video e {sum(v["vectors"] for v in selected)} embedding dall’archivio?')
        box.setInformativeText('I file video originali rimangono sul disco. Verranno eliminate solo le voci selezionate, '
                               'i loro embedding e le forme d’onda. Potrai aggiungere nuovamente i video in seguito.')
        box.setDetailedText('\n'.join(v['path'] for v in selected))
        remove = box.addButton('Rimuovi dall’archivio', QMessageBox.ButtonRole.DestructiveRole)
        cancel = box.addButton('Annulla', QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(cancel)
        box.setEscapeButton(cancel)
        box.exec()
        if box.clickedButton() != remove:
            return
        def done(result):
            self.results = []
            self.result_list.clear()
            self.clear_player()
            self.search_summary.setText('Archivio aggiornato. Avvia una nuova ricerca.')
            self.task_result(result)
            self.import_summary.setText(result['message'])
        self.start_task(lambda stop, notify: self.library.remove(ids), done)

    def index(self, selected=False):
        ids = None
        if selected:
            ids = self.selected_ids()
            if not ids:
                self.status.setText('Seleziona almeno un video nella tabella.')
                return
        candidates = [v for v in self.library.videos() if (ids is None or v['id'] in ids) and Path(v['path']).is_file()]
        if not candidates:
            self.status.setText('Nessun file disponibile. Aggiungi i video dalla loro cartella attuale.')
            return
        ids = [v['id'] for v in candidates]
        engine = self.get_engine()
        step = self.step.currentData()
        self.start_task(lambda stop, notify: self.library.index(engine, step, stop, notify, ids))

    def search(self):
        if self.worker:
            self.status.setText('Sospendi l’elaborazione corrente prima di avviare una ricerca.')
            return
        query = self.query.text().strip()
        if not query:
            self.query.setFocus()
            return
        videos = self.library.videos()
        if not any(v['vectors'] for v in videos):
            self.search_summary.setText('Non ci sono ancora embedding: analizza almeno un video nella scheda Indicizzazione.')
            return
        if not any(v['vectors'] and Path(v['path']).is_file() for v in videos):
            self.results = []
            self.result_list.clear()
            self.clear_player()
            self.search_summary.setText('File video non disponibili. Ricollega il supporto oppure aggiungi la cartella in cui li hai spostati.')
            return
        engine = self.get_engine()
        mode, minimum = self.mode.currentData(), self.minimum.value()
        def work(stop, notify):
            engine.start(stop, lambda message: notify(dict(message=message)))
            notify(dict(message='Ricerca negli intervalli analizzati…'))
            return self.library.search(engine.query(query, stop), mode=mode, minimum=minimum, stop=stop)
        self.start_task(work, self.show_results)

    def show_results(self, results):
        self.results = results
        self.result_list.blockSignals(True)
        self.result_list.clear()
        for result in results:
            intervals = result['intervals']
            item = QListWidgetItem(f'{result["name"]}\n{len(intervals)} intervalli · somiglianza {result["score"]:.3f}\n'
                                   + '  ·  '.join(duration(x['start']) for x in intervals[:3])
                                   + (' …' if len(intervals) > 3 else '')
                                   + ('\nFile non collegato' if not result['available'] else ''))
            item.setToolTip(result['path'])
            self.result_list.addItem(item)
        self.result_list.blockSignals(False)
        count = sum(len(r['intervals']) for r in results)
        videos = self.library.videos()
        missing = sum(not Path(v['path']).is_file() for v in videos)
        partial = sum(v['checkpoint'] < v['duration'] - 1e-6 and Path(v['path']).is_file() for v in videos)
        text = f'{len(results)} video · {count} intervalli tra i più simili (massimo 30).'
        if partial:
            text += f' Indice parziale: {partial} video ancora da completare.'
        if not results:
            text = 'Nessun intervallo supera la soglia. Prova una descrizione diversa o abbassa la somiglianza minima.'
        if missing:
            text += f' {missing} file mancanti esclusi dalla ricerca.'
        self.search_summary.setText(text)
        self.status.setText('Ricerca completata. Puoi cambiare risultato anche durante la riproduzione.')
        self.progress.setRange(0, 1000)
        self.progress.setValue(1000)
        if results:
            self.result_list.setCurrentRow(0)
        else:
            self.clear_player()

    def select_result(self, row):
        if 0 <= row < len(self.results):
            self.load_result(self.results[row])

    def retry_result(self, item):
        # A reconnected drive can be retried without changing the search/selection.
        if not self.play_button.isEnabled():
            self.select_result(self.result_list.row(item))

    def load_result(self, result):
        self.player.stop()
        self.player.setSource(QUrl())
        self.pending_seek = None
        self.current_result = result
        self.video_title.setText(result['name'])
        self.play_button.setEnabled(False)
        self.slider.setRange(0, round(result['duration'] * 1000))
        self.slider.setValue(0)
        self.intervals.clear()
        intervals = result.get('intervals', [])
        for i, interval in enumerate(intervals):
            evidence = ' + '.join('audio' if x == 'audio' else 'immagini' for x in sorted(interval['modalities']))
            self.intervals.addItem(f'{i + 1}. {duration(interval["start"])} – {duration(interval["end"])} · {evidence}', interval['start'])
        self.intervals.setEnabled(bool(intervals))
        best_interval = max(range(len(intervals)), key=lambda i: intervals[i].get('score', 0)) if intervals else 0
        if intervals:
            self.intervals.setCurrentIndex(best_interval)
        self.previous.setEnabled(len(intervals) > 1)
        self.next.setEnabled(len(intervals) > 1)
        peaks = np.empty((0, 2))
        wave = self.library.waveform_path(result['video_id'])
        if wave.exists():
            try:
                with np.load(wave, allow_pickle=False) as data:
                    peaks = data['peaks']
            except (ValueError, OSError, KeyError):
                pass
        message = 'Forma d’onda disponibile dopo l’analisi audio.' if result.get('has_audio') else 'Video senza traccia audio.'
        self.waveform.set_media(result['duration'], peaks, intervals, message)
        path = Path(result['path'])
        if not path.is_file():
            self.playback_note.setText('File non disponibile. Ricollega la chiavetta e seleziona di nuovo questo risultato.')
            return
        try:
            if result.get('fingerprint') and fingerprint(path) != result['fingerprint']:
                self.playback_note.setText('Il file è cambiato: rileggi la cartella e analizzalo nuovamente.')
                return
        except OSError as e:
            self.playback_note.setText(str(e))
            return
        self.pending_seek = round((intervals[best_interval]['start'] if intervals else 0) * 1000)
        self.playback_note.setText('Intervalli stimati. Riproduzione libera sull’intera durata; clic sulla traccia per spostarti.')
        self.player.setSource(QUrl.fromLocalFile(str(path)))
        self.play_button.setEnabled(True)

    def media_status(self, status):
        if status in (QMediaPlayer.MediaStatus.LoadedMedia, QMediaPlayer.MediaStatus.BufferedMedia):
            if self.pending_seek is not None:
                position, self.pending_seek = self.pending_seek, None
                self.player.setPosition(position)
        if status == QMediaPlayer.MediaStatus.EndOfMedia:
            self.play_button.setText('Riproduci')

    def playback_error(self, error, message):
        if error != QMediaPlayer.Error.NoError:
            self.playback_note.setText('Riproduzione non disponibile: ' + message)
            self.play_button.setEnabled(False)

    def toggle_play(self):
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
        elif self.player.source().isValid():
            self.player.play()

    def play_state(self, state):
        self.play_button.setText('Pausa' if state == QMediaPlayer.PlaybackState.PlayingState else 'Riproduci')

    def position_changed(self, ms):
        if not self.slider.isSliderDown():
            self.slider.setValue(ms)
        self.waveform.set_position(ms / 1000)
        seconds = self.current_result['duration'] if self.current_result else self.player.duration() / 1000
        self.time_label.setText(f'{duration(ms / 1000)} / {duration(seconds)}')

    def jump_interval(self, index):
        seconds = self.intervals.itemData(index)
        if seconds is not None:
            self.player.setPosition(round(seconds * 1000))

    def change_interval(self, direction):
        if self.intervals.count():
            index = (self.intervals.currentIndex() + direction) % self.intervals.count()
            self.intervals.setCurrentIndex(index)
            self.jump_interval(index)

    def preview_selected(self, item):
        ident = item.data(Qt.ItemDataRole.UserRole)
        video = next(v for v in self.library.videos() if v['id'] == ident)
        self.tabs.setCurrentIndex(1)
        self.load_result(dict(video, video_id=ident, intervals=[]))

    def clear_player(self):
        self.player.stop()
        self.player.setSource(QUrl())
        self.current_result = None
        self.pending_seek = None
        self.play_button.setEnabled(False)
        self.video_title.setText('Seleziona un risultato')
        self.waveform.set_media(0, np.empty((0, 2)), [], 'Seleziona un risultato per vedere la traccia audio.')
        self.intervals.clear()
        self.slider.setRange(0, 0)

    def confirm_reset(self):
        if self.worker:
            return
        count = sum(v['vectors'] for v in self.library.videos())
        if not count:
            self.status.setText('Non ci sono embedding da eliminare.')
            return
        dialog = QMessageBox(self)
        dialog.setWindowTitle('Eliminare gli embedding dei video?')
        dialog.setIcon(QMessageBox.Icon.Warning)
        dialog.setText(f'Vuoi eliminare tutti i {count} embedding di GemmaVideo?')
        dialog.setInformativeText('I video originali e le forme d’onda restano disponibili. '
                                  'Per eseguire nuove ricerche sarà necessario ripetere l’analisi.')
        delete = dialog.addButton('Elimina tutti', QMessageBox.ButtonRole.DestructiveRole)
        cancel = dialog.addButton('Annulla', QMessageBox.ButtonRole.RejectRole)
        dialog.setDefaultButton(cancel)
        dialog.setEscapeButton(cancel)
        dialog.exec()
        if dialog.clickedButton() != delete:
            return
        self.results = []
        self.result_list.clear()
        self.clear_player()
        self.search_summary.setText('Embedding eliminati. Analizza i video per creare un nuovo indice.')
        def reset(stop, notify):
            if self.engine:
                self.engine.close()
            count = self.library.reset()
            return dict(message=f'{count} embedding eliminati. I video sono pronti per una nuova analisi.')
        self.start_task(reset)

    def show_errors(self):
        box = QMessageBox(self)
        box.setWindowTitle('Dettagli elaborazione')
        box.setText(self.last_errors[0][:700] if self.last_errors else 'Nessun errore.')
        box.setDetailedText('\n\n'.join(self.last_errors))
        box.exec()

    def closeEvent(self, event):
        if self.worker:
            self.closing = True
            self.cancel()
            self.status.setText('Chiusura in corso; salvataggio del lavoro completato…')
            event.ignore()
            return
        self.player.stop()
        self.player.setSource(QUrl())
        if self.engine:
            self.engine.close()
        event.accept()


def main():
    parser = argparse.ArgumentParser(description='GemmaVideo · ricerca locale in audio e fotogrammi')
    parser.add_argument('--data', type=Path, default=APP / 'dati')
    parser.add_argument('--source', type=Path, help='Cartella iniziale; normalmente viene ricordata l’ultima usata')
    parser.add_argument('--backend', choices=['cpu', 'vulkan'])
    parser.add_argument('--silent', action='store_true', help='Disabilita l’uscita audio, utile senza una scheda audio')
    args = parser.parse_args()
    app = QApplication(sys.argv[:1])
    app.setApplicationName('GemmaVideo')
    QFontDatabase.addApplicationFont(str(ROOT / 'archivio_gemma/assets/C64Keyboard-Regular.ttf'))
    app.setStyleSheet(STYLE)
    args.data.mkdir(parents=True, exist_ok=True)
    lock = QLockFile(str(args.data / 'app.lock'))
    lock.setStaleLockTime(0)
    if not lock.tryLock(0):
        QMessageBox.information(None, 'GemmaVideo', 'GemmaVideo è già aperto su questo archivio.')
        return 1
    window = Window(args.data, args.source, args.backend, audio_enabled=not args.silent)
    window.show()
    result = app.exec()
    lock.unlock()
    return result


if __name__ == '__main__':
    raise SystemExit(main())
