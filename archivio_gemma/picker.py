"""Lightweight multi-file picker for large research corpora; no PDF previews."""
import os
import threading
from pathlib import Path
from PySide6.QtCore import Qt,QThread,Signal,QStringListModel,QSortFilterProxyModel,QTimer
from PySide6.QtWidgets import (QDialog,QVBoxLayout,QHBoxLayout,QLabel,QLineEdit,
    QPushButton,QListView,QFileDialog,QAbstractItemView)
from core import SUPPORTED

class DirectoryScan(QThread):
    found=Signal(object);failure=Signal(str)
    def __init__(self,directory,parent=None):
        super().__init__(parent);self.directory=Path(directory);self.stop=threading.Event()
    def run(self):
        try:
            names=[]
            # DirEntry uses directory metadata; do not open/stat every PDF for icons.
            with os.scandir(self.directory) as entries:
                for entry in entries:
                    if self.stop.is_set():return
                    if Path(entry.name).suffix.lower() in SUPPORTED and entry.is_file():names.append(entry.name)
            names.sort(key=str.casefold)
            if not self.stop.is_set():self.found.emit((self.directory,names))
        except OSError as error:self.failure.emit(str(error))

class DocumentPicker(QDialog):
    def __init__(self,directory,parent=None):
        super().__init__(parent);self.setWindowTitle('Aggiungi documenti');self.resize(860,680)
        self.directory=Path(directory);self.scan=None;self.paths=[];self.closing=False
        box=QVBoxLayout(self);box.setContentsMargins(18,18,18,18);box.setSpacing(12)
        title=QLabel('SCEGLI I DOCUMENTI');title.setObjectName('section');box.addWidget(title)
        row=QHBoxLayout();self.location=QLineEdit(str(self.directory));self.location.returnPressed.connect(self.load)
        row.addWidget(self.location);self.browse=QPushButton('Cambia cartella');self.browse.clicked.connect(self.browse_directory);row.addWidget(self.browse);box.addLayout(row)
        self.filter=QLineEdit();self.filter.setPlaceholderText('Filtra per nome, per esempio w1584 oppure .pdf');box.addWidget(self.filter)
        self.model=QStringListModel(self);self.proxy=QSortFilterProxyModel(self);self.proxy.setSourceModel(self.model)
        self.proxy.setFilterCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.view=QListView();self.view.setModel(self.proxy);self.view.setUniformItemSizes(True)
        self.view.setLayoutMode(QListView.LayoutMode.Batched);self.view.setBatchSize(300)
        self.view.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers);box.addWidget(self.view,1)
        self.count=QLabel();self.count.setObjectName('muted');box.addWidget(self.count)
        note=QLabel('Ctrl + clic: selezione multipla · Maiusc + clic: intervallo\nL’elenco mostra solo i nomi; nessun documento viene aperto durante la selezione.')
        note.setObjectName('muted');box.addWidget(note)
        actions=QHBoxLayout();actions.addStretch();self.cancel=QPushButton('Annulla');self.cancel.clicked.connect(self.reject);actions.addWidget(self.cancel)
        self.confirm=QPushButton('Aggiungi selezionati');self.confirm.setObjectName('primary');self.confirm.setEnabled(False);self.confirm.clicked.connect(self.accept_selection);actions.addWidget(self.confirm);box.addLayout(actions)
        self.delay=QTimer(self);self.delay.setSingleShot(True);self.delay.setInterval(100);self.delay.timeout.connect(self.apply_filter)
        self.filter.textChanged.connect(lambda:self.delay.start());self.view.selectionModel().selectionChanged.connect(self.selection_changed)
        self.view.doubleClicked.connect(self.accept_selection)
        QTimer.singleShot(0,self.load)
    def load(self):
        if self.scan:return
        directory=Path(self.location.text()).expanduser()
        self.model.setStringList([]);self.count.setText('Lettura dell’elenco in corso…');self.confirm.setEnabled(False)
        self.location.setEnabled(False);self.browse.setEnabled(False)
        self.scan=DirectoryScan(directory,self);self.scan.found.connect(self.loaded);self.scan.failure.connect(self.failed);self.scan.finished.connect(self.scan_finished);self.scan.start()
    def loaded(self,result):
        self.directory,names=result;self.model.setStringList(names);self.apply_filter()
    def failed(self,message):self.count.setText('Impossibile leggere la cartella: '+message)
    def scan_finished(self):
        self.scan.deleteLater();self.scan=None;self.location.setEnabled(True);self.browse.setEnabled(True)
        if self.closing:super().reject()
    def apply_filter(self):
        self.proxy.setFilterFixedString(self.filter.text());self.selection_changed()
    def selection_changed(self,*args):
        n=len(self.view.selectionModel().selectedRows());self.confirm.setEnabled(n>0 and self.scan is None)
        self.count.setText(f'{self.model.rowCount():,} documenti · {self.proxy.rowCount():,} visibili · {n} selezionati'.replace(',','.'))
    def browse_directory(self):
        directory=QFileDialog.getExistingDirectory(self,'Scegli cartella',str(self.directory),QFileDialog.Option.ShowDirsOnly|QFileDialog.Option.DontUseNativeDialog|QFileDialog.Option.DontUseCustomDirectoryIcons)
        if directory:self.location.setText(directory);self.load()
    def accept_selection(self,*args):
        if self.scan:return
        self.paths=[str(self.directory/index.data()) for index in self.view.selectionModel().selectedRows()]
        if self.paths:self.accept()
    def reject(self):
        if self.scan:
            self.closing=True;self.scan.stop.set();self.count.setText('Chiusura…');return
        super().reject()
    def closeEvent(self,event):
        if self.scan:self.reject();event.ignore()
        else:super().closeEvent(event)
