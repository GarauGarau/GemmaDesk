"""GemmaDesk native desktop interface, styled after Paperdesk."""
import argparse, html, json, os, sys, threading, time
from collections import Counter
from pathlib import Path
from PySide6.QtCore import Qt,QThread,Signal,QTimer,QUrl,QLockFile,QPointF,QStringListModel,QSortFilterProxyModel,QSize
from PySide6.QtGui import QColor,QFont,QFontDatabase,QPainter,QPen,QBrush,QDesktopServices,QIcon,QPixmap,QShortcut,QKeySequence
from PySide6.QtWidgets import (QApplication,QMainWindow,QWidget,QVBoxLayout,QHBoxLayout,QLabel,QPushButton,
    QFileDialog,QMessageBox,QLineEdit,QComboBox,QSplitter,QFrame,QTableWidget,QTableWidgetItem,
    QHeaderView,QAbstractItemView,QProgressBar,QTextBrowser,QDialog,QGraphicsView,QGraphicsScene,QGraphicsEllipseItem,QSpinBox,QListView,QListWidget,QListWidgetItem,QCheckBox,QScrollArea,QTabWidget)
from core import Archive,EnginePool,Cancelled,APP,DEFAULT_CORPUS,SUPPORTED,check
from picker import DocumentPicker
from map_style import COLORS,group_label,group_color
STYLE='''
QWidget {background:#e9e5da;color:#25333c;font-family:"DejaVu Sans";font-size:13px;}
QMainWindow {background:#e9e5da;}
QFrame#panel {border:1px solid #c1c2b7;background:#f6f3e9;}
QLabel {background:transparent;}
QLabel#brand {font-family:"C64 Keyboard";font-size:34px;}
QLabel#section {font-family:"C64 Keyboard";font-size:17px;}
QLabel#muted {color:#66706e;}
QLabel#number {font-family:"C64 Keyboard";font-size:24px;}
QPushButton {background:#f6f3e9;border:1px solid #aaa99b;border-bottom:4px solid #a4a392;padding:9px 15px;font-family:"C64 Keyboard";font-size:14px;}
QPushButton:hover {background:#fffdf8;border-color:#538992;}
QPushButton:pressed {border-bottom:1px solid #a4a392;padding-top:12px;}
QPushButton:disabled {color:#92968e;background:#e0ded4;}
QPushButton#primary {background:#538992;color:#fffdf8;border-color:#3d6971;}
QLineEdit,QComboBox,QSpinBox {background:#fffdf8;border:1px solid #c1c2b7;padding:8px;selection-background-color:#538992;}
QComboBox QAbstractItemView {background:#fffdf8;}
QProgressBar {border:1px solid #c1c2b7;background:#d0d1ca;height:9px;text-align:center;}
QProgressBar::chunk {background:#538992;}
QTableWidget,QTextBrowser,QListView {background:#f6f3e9;border:1px solid #c1c2b7;selection-background-color:#d1e1df;selection-color:#25333c;}
QHeaderView::section {background:#deded3;border:0;border-bottom:1px solid #c1c2b7;padding:7px;text-align:left;}
QListView::item {padding:5px;}
QTableWidget::item {padding:5px;border-bottom:1px solid #e1dfd4;}
QSplitter::handle {background:#c1c2b7;margin:4px;}
QToolTip {background:#fffdf8;color:#25333c;border:1px solid #538992;padding:6px;}
QScrollBar:vertical {width:12px;background:#e9e5da;}
QScrollBar::handle:vertical {background:#babeb4;min-height:24px;}
'''

def duration(seconds):
    if seconds is None:return 'In stima…'
    seconds=max(0,int(seconds));h,seconds=divmod(seconds,3600);m,s=divmod(seconds,60)
    return f'{h:02d}:{m:02d}:{s:02d}'

def open_document(path,page=1):
    url=QUrl.fromLocalFile(str(path))
    if str(path).lower().endswith('.pdf'):url.setFragment('page='+str(page))
    QDesktopServices.openUrl(url)

def partial_note(item):
    if not item.get('partial'):return ''
    return f'Documento parziale · {item["done"]} / {item.get("total_parts") or "?"} parti/pagine salvate'

class Task(QThread):
    progress=Signal(object);result=Signal(object);failure=Signal(str);paused=Signal(str)
    def __init__(self,fn):super().__init__();self.fn=fn;self.stop=threading.Event()
    def run(self):
        try:self.result.emit(self.fn(self.stop,self.progress.emit))
        except Cancelled as e:self.paused.emit(str(e))
        except Exception as e:self.failure.emit(str(e))

class Dot(QGraphicsEllipseItem):
    def __init__(self,item,diameter=16):
        super().__init__(-diameter/2,-diameter/2,diameter,diameter);self.item=item
        self.setPos(item['x']*550,-item['y']*550)
        self.setBrush(QColor(group_color(item['cluster'])));self.setPen(QPen(QColor('#f6f3e9'),1))
        self.setFlag(self.GraphicsItemFlag.ItemIgnoresTransformations)
        self.setFlag(self.GraphicsItemFlag.ItemIsSelectable)
        passage=f'<br>Passaggio / pagina {item["page"]}: '+html.escape(item.get('text','')[:450]) if item.get('chunk_id') else ''
        partial='<br>'+partial_note(item) if item.get('partial') else ''
        context='<br>Argomento del resoconto: '+html.escape(item['topic_context'][:600]) if item.get('topic_context') else ''
        self.setToolTip(html.escape(item['name'])+partial+context+passage+'<br>Doppio clic: apri · clic destro: punti sovrapposti')
    def mouseDoubleClickEvent(self,event):open_document(self.item['path'],self.item.get('page',1));event.accept()

class Map(QGraphicsView):
    chosen=Signal(object)
    def __init__(self):
        super().__init__();self.setScene(QGraphicsScene(self));self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setBackgroundBrush(QColor('#f6f3e9'));self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setDragMode(self.DragMode.ScrollHandDrag);self.setTransformationAnchor(self.ViewportAnchor.AnchorUnderMouse)
        self.setMinimumHeight(120);self.items_data=[];self.matches=set();self.match_chunks=None;self.dots={};self.selected_id=None
        self.active_group=None
        self.cloud=None;self._navigated=False;self._press=None;self._hover_time=0
        self.setMouseTracking(True)
        self.scene().selectionChanged.connect(self.selection)
    def drawBackground(self,painter,rect):
        painter.fillRect(rect,QColor('#f6f3e9'))
        pen=QPen(QColor('#e5e1d6'));pen.setCosmetic(True);painter.setPen(pen)
        # Fine dot grid in viewport coordinates remains restrained at every zoom.
        painter.save();painter.resetTransform()
        for x in range(18,self.viewport().width(),30):
            for y in range(18,self.viewport().height(),30):painter.drawPoint(x,y)
        painter.restore()
    def set_data(self,items):
        selected=self.selected_id;self.scene().blockSignals(True)
        self.items_data=items;self.dots={};self.scene().clear();self.cloud=None
        if len(items)>1000:
            from point_cloud import PointCloud
            self.cloud=PointCloud(items);self.scene().addItem(self.cloud)
        else:
            for item in items:
                dot=Dot(item,10 if len(items)>300 else 16);self.scene().addItem(dot);self.dots[item.get('point_id',item['id'])]=dot
        if self.active_group not in {item['cluster'] for item in items}:self.active_group=None
        if not items:
            text=self.scene().addText('La tua biblioteca, in una mappa.\n\nAggiungi documenti e calcola gli embedding.\nOgni punto rappresenterà un documento.',QFont('DejaVu Sans',14))
            text.setDefaultTextColor(QColor('#66706e'));text.setPos(-230,-65)
        self.matches.intersection_update(item['id'] for item in items)
        self.scene().blockSignals(False);self.select_document(selected)
        self.reset_view()
    def reset_view(self):
        self._navigated=False
        pad=8 if self.cloud else 55
        rect=self.scene().itemsBoundingRect().adjusted(-pad,-pad,pad,pad)
        self.setSceneRect(rect);self.fitInView(rect,Qt.AspectRatioMode.KeepAspectRatio)
        if self.transform().m11()>3:self.resetTransform();self.centerOn(rect.center())
    def resizeEvent(self,event):
        super().resizeEvent(event)
        if getattr(self,'items_data',None) and not self._navigated:self.reset_view()
    def wheelEvent(self,event):
        factor=1.18 if event.angleDelta().y()>0 else 1/1.18
        if .005<self.transform().m11()*factor<100:self.scale(factor,factor);self._navigated=True
        event.accept()
    def selection(self):
        if self.cloud:return
        selected=[dot for dot in self.scene().selectedItems() if isinstance(dot,Dot)]
        self.selected_id=selected[0].item['id'] if selected else None
        self._style_dots()
        if selected:self.chosen.emit(selected[0].item)
    def select_document(self,id,emit=False):
        if self.cloud:
            indices=self.cloud.documents.get(id,[]);self.selected_id=id if indices else None
            self._style_dots()
            if emit and indices:self.chosen.emit(self.items_data[indices[0]])
            return
        self.scene().blockSignals(True);self.scene().clearSelection()
        dots=[dot for dot in self.dots.values() if dot.item['id']==id]
        self.selected_id=id if dots else None
        if dots:dots[0].setSelected(True)
        self.scene().blockSignals(False);self._style_dots()
        if emit and dots:self.chosen.emit(dots[0].item)
    def documents_at(self,position):
        if self.cloud:
            scene=self.mapToScene(position);radius=6/max(self.transform().m11(),1e-6)
            return [self.items_data[i] for i in self.cloud.hits(scene.x(),scene.y(),radius)]
        return sorted((dot.item for dot in self.items(position) if isinstance(dot,Dot)),key=lambda item:(item['name'],item.get('page',1),item.get('chunk_id') or 0))

    def mousePressEvent(self,event):
        self._press=event.position().toPoint();super().mousePressEvent(event)

    def mouseMoveEvent(self,event):
        if event.buttons():self._navigated=True
        elif self.cloud and time.monotonic()-self._hover_time>.12:
            self._hover_time=time.monotonic();items=self.documents_at(event.position().toPoint())
            from PySide6.QtWidgets import QToolTip
            if items:
                item=items[0]
                QToolTip.showText(event.globalPosition().toPoint(),html.escape(item['name'])+'<br>'+html.escape(' · '.join(item.get('topics',[])))+'<br>'+html.escape(item.get('text','')[:350])+'<br>Clic destro: tutti i passaggi sovrapposti',self)
            else:QToolTip.hideText()
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self,event):
        super().mouseReleaseEvent(event)
        if self.cloud and event.button()==Qt.MouseButton.LeftButton and self._press is not None and (event.position().toPoint()-self._press).manhattanLength()<5:
            items=self.documents_at(event.position().toPoint())
            if items:self.select_document(items[0]['id']);self.chosen.emit(items[0])

    def mouseDoubleClickEvent(self,event):
        if self.cloud:
            items=self.documents_at(event.position().toPoint())
            if items:open_document(items[0]['path'],items[0].get('page',1));event.accept();return
        super().mouseDoubleClickEvent(event)
    def contextMenuEvent(self,event):
        items=self.documents_at(event.pos())
        if not items:return
        # A searchable list stays usable even with thousands of coincident points.
        # Overlapping points keep their projected coordinates, without jitter.
        dialog=QDialog(self);dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.setWindowTitle('Documenti e passaggi in questo punto');dialog.resize(600,420)
        layout=QVBoxLayout(dialog);layout.addWidget(QLabel(f'{len(items)} punti sotto il cursore · scegli quale selezionare o aprire'))
        query=QLineEdit();query.setPlaceholderText('Filtra per nome…');layout.addWidget(query)
        model=QStringListModel([f'{item["name"]} · {group_label(item["cluster"])} · parte/pagina {item.get("page",1)} · #{item.get("point_id",item["id"])}' for item in items],dialog)
        proxy=QSortFilterProxyModel(dialog);proxy.setSourceModel(model);proxy.setFilterCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        query.textChanged.connect(proxy.setFilterFixedString)
        view=QListView();view.setModel(proxy);view.setUniformItemSizes(True);layout.addWidget(view)
        row=QHBoxLayout();choose=QPushButton('Seleziona');openbutton=QPushButton('Apri documento');row.addWidget(choose);row.addWidget(openbutton);layout.addLayout(row)
        def pick(open_file=False):
            current=view.currentIndex()
            if not current.isValid():return
            item=items[proxy.mapToSource(current).row()];self.select_document(item['id']);self.chosen.emit(item)
            if open_file:open_document(item['path'],item.get('page',1))
            dialog.accept()
        choose.clicked.connect(lambda:pick());openbutton.clicked.connect(lambda:pick(True));view.doubleClicked.connect(lambda index:pick(True))
        view.setCurrentIndex(proxy.index(0,0));dialog.open();event.accept()
    def highlight(self,ids,chunk_ids=None):
        was_group=self.active_group is not None
        self.active_group=None
        self.matches=set(ids);self.match_chunks=set(chunk_ids) if chunk_ids is not None else None
        self._style_dots()
        if was_group:self.reset_view()
    def highlight_group(self,group):
        if self.cloud:
            self.matches=set();self.match_chunks=None;self.selected_id=None;self.active_group=group;self._style_dots();self.focus_group(group);return
        self.highlight([])
        self.select_document(None)
        self.active_group=group
        self._style_dots()

    def focus_group(self,group):
        if not self.cloud:return
        indices=self.cloud.groups.get(group,[])
        if not indices:return
        from PySide6.QtCore import QRectF
        xy=self.cloud.xy[indices];low=xy.min(axis=0);high=xy.max(axis=0)
        # Fit every member, without moving points or hiding distant members.
        rect=QRectF(float(low[0]),float(low[1]),max(5.,float(high[0]-low[0])),max(5.,float(high[1]-low[1]))).adjusted(-10,-10,10,10)
        self.fitInView(rect,Qt.AspectRatioMode.KeepAspectRatio);self._navigated=True
    def _style_dots(self):
        if self.cloud:
            self.cloud.style(self.active_group,self.matches,self.match_chunks,self.selected_id);return
        for dot in self.dots.values():
            id=dot.item['id'];chunk=dot.item.get('chunk_id')
            matched=id in self.matches and (chunk is None or self.match_chunks is None or chunk in self.match_chunks)
            active=dot.item['cluster']==self.active_group if self.active_group is not None else matched or id==self.selected_id
            pen=QPen(QColor('#25333c' if active or dot.item.get('partial') else '#f6f3e9'),2.5 if active else 1)
            if dot.item.get('partial'):pen.setStyle(Qt.PenStyle.DashLine)
            dot.setPen(pen)
            filtered=self.active_group is not None or bool(self.matches)
            dot.setOpacity(1 if not filtered or active else .22)
            dot.setZValue(2 if id==self.selected_id else 1 if active else 0)

class Window(QMainWindow):
    def __init__(self,archive,corpus=DEFAULT_CORPUS,collection='My documents'):
        super().__init__();self.archive=archive;self.engine=None;self.task=None;self.closing=False;self.stats={};self.started=None
        self.corpus=Path(corpus);self.collection=collection
        from timeline import available
        self.is_camera=available(archive);self.timeline_data=None;self.timeline_window=None;self.timeline_button=None
        with archive.connect() as db:self.stable_camera=bool(db.execute("SELECT 1 FROM meta WHERE (key='camera_preprocessing' AND value='camera-thematic-v2') OR (key='thematic_preprocessing' AND value='essays-no-overlap-v1')").fetchone())
        self.history_catalog={'sessions':[]}
        self.settings={b:dict(workers=1,batch_size=4) for b in ['cpu','vulkan']}
        self.settings['cpu']=dict(workers=2,batch_size=2)
        try:
            saved=json.loads((archive.data/'preferenze.json').read_text())
            for backend in self.settings:
                entry=saved.get(backend,{}) if isinstance(saved,dict) else {}
                if isinstance(entry,dict) and entry.get('workers') in (1,2) and entry.get('batch_size') in (1,2,4,8):
                    self.settings[backend]=dict(workers=entry['workers'],batch_size=entry['batch_size'])
        except (OSError,ValueError,TypeError):pass
        self.setWindowTitle('GemmaDesk · '+self.collection);self.resize(1460,960);self.setMinimumSize(1080,750);self.setAcceptDrops(True)
        central=QWidget();self.setCentralWidget(central);layout=QVBoxLayout(central);layout.setContentsMargins(24,14,24,14);layout.setSpacing(9)
        self.header=QWidget();top=QHBoxLayout(self.header);top.setContentsMargins(0,0,0,0);brand=QLabel('GEMMADESK');brand.setObjectName('brand');top.addWidget(brand)
        top.addStretch()
        self.backend=QComboBox();self.backend.addItems(['GPU Vulkan','CPU']);top.addWidget(self.backend)
        self.gpubutton=QPushButton('Verifica GPU');self.gpubutton.clicked.connect(self.verify_gpu);top.addWidget(self.gpubutton)
        helpbutton=QPushButton('Limiti e metodo');helpbutton.clicked.connect(self.help);top.addWidget(helpbutton);layout.addWidget(self.header)
        self.separator=QFrame();self.separator.setFrameShape(QFrame.Shape.HLine);layout.addWidget(self.separator)
        self.index_controls=QWidget();bar=QHBoxLayout(self.index_controls);bar.setContentsMargins(0,0,0,0)
        self.addbutton=QPushButton('+ Aggiungi documenti');self.addbutton.clicked.connect(self.pick);bar.addWidget(self.addbutton)
        self.corpusbutton=QPushButton('Raccolta '+self.collection);self.corpusbutton.clicked.connect(self.add_corpus);bar.addWidget(self.corpusbutton)
        bar.addStretch()
        self.stats_toggle=QCheckBox('Statistiche');bar.addWidget(self.stats_toggle)
        self.startbutton=QPushButton('Calcola embedding');self.startbutton.setObjectName('primary');self.startbutton.clicked.connect(self.index);bar.addWidget(self.startbutton)
        self.stopbutton=QPushButton('Sospendi');self.stopbutton.setEnabled(False);self.stopbutton.clicked.connect(self.cancel);bar.addWidget(self.stopbutton);layout.addWidget(self.index_controls)
        self.stats_panel=QWidget();stats_layout=QVBoxLayout(self.stats_panel);stats_layout.setContentsMargins(0,0,0,0);stats_layout.setSpacing(9)
        metrics=QHBoxLayout();self.metrics={}
        for key,title in [('elapsed','TEMPO TRASCORSO'),('done','DOCUMENTI PROCESSATI'),('remaining','DOCUMENTI RIMANENTI'),('eta','TEMPO RIMANENTE')]:
            panel=QFrame();panel.setObjectName('panel');box=QVBoxLayout(panel);label=QLabel(title);label.setObjectName('muted');box.addWidget(label)
            val=QLabel('00:00:00' if key=='elapsed' else '—');val.setObjectName('number');box.addWidget(val);self.metrics[key]=val;metrics.addWidget(panel)
        stats_layout.addLayout(metrics)
        perf=QFrame();perf.setObjectName('panel');perf.setToolTip('Prestazioni dell’elaborazione documenti corrente o appena terminata. I rapporti conservano il backend effettivo della sessione.');perfrow=QHBoxLayout(perf);self.perf={}
        for key,title in [('pages','PARTI / PAGINE SALVATE'),('pps','PARTI / PAGINE AL SEC.'),('vectors','EMBEDDING CALCOLATI'),('eps','EMBEDDING / SECONDO'),('load','CARICAMENTO MODELLO'),('ram','RAM DEL MODELLO')]:
            col=QVBoxLayout();label=QLabel(title);label.setObjectName('muted');label.setStyleSheet('font-size:11px');col.addWidget(label)
            val=QLabel('—');val.setStyleSheet('font-family:"C64 Keyboard";font-size:19px');col.addWidget(val);self.perf[key]=val;perfrow.addLayout(col)
        stats_layout.addWidget(perf);layout.addWidget(self.stats_panel)
        self.stats_toggle.toggled.connect(lambda checked:self.stats_panel.setVisible(checked and self.tabs.currentIndex()==0));self.stats_panel.hide()
        perf.setToolTip('Per TXT/MD si contano passaggi di massimo 1.024 token; i testi più brevi restano interi. Per PDF si contano le pagine. I tassi escludono il caricamento iniziale. RAM: somma dei processi del modello.')
        self.backendlabel=QLabel('Dispositivo non ancora verificato · GPU Vulkan selezionata · nessuna elaborazione avviata')
        self.backendlabel.setWordWrap(True);self.backendlabel.setObjectName('muted');layout.addWidget(self.backendlabel)
        self.status=QLabel('');self.status.setWordWrap(True);layout.addWidget(self.status)
        self.progress=QProgressBar();self.progress.setTextVisible(False);self.progress.setRange(0,100);self.progress.setValue(0);layout.addWidget(self.progress)
        self.tabs=QTabWidget();layout.addWidget(self.tabs,1)
        self.tabs.setAccessibleName('Viste di GemmaDesk')
        self.tabs.currentChanged.connect(self.tab_changed)
        self.map_splitter=QSplitter(Qt.Orientation.Horizontal);splitter=self.map_splitter
        self.map_frame=left=QFrame();left.setObjectName('panel');ll=QVBoxLayout(left)
        self.map_controls=QWidget();controls=QVBoxLayout(self.map_controls);controls.setContentsMargins(0,0,0,0);lh=QHBoxLayout();label=QLabel('MAPPA SEMANTICA');label.setObjectName('section');lh.addWidget(label);lh.addStretch()
        self.clusters=QSpinBox();self.clusters.setRange(0,len(COLORS));self.clusters.setSpecialValueText('Gruppi: auto');self.clusters.setPrefix('Gruppi: ');self.clusters.setToolTip('0 = automatico. Aumentare il numero suddivide un gruppo esistente: gli altri mantengono documenti, nomi e colori. Ridurlo riunisce gli ultimi sottogruppi. Vale a parità di biblioteca e vista. Premi Ridisegna per applicare.');lh.addWidget(self.clusters)
        if self.stable_camera:
            self.clusters.setEnabled(False);self.clusters.setSpecialValueText('Temi scoperti');self.clusters.setToolTip('Numero e nomi dei temi sono scoperti dai testi. Grigio: passaggi da verificare. Dettagli in Limiti e metodo.')
            self.clusters.setPrefix('');self.clusters.setMinimumWidth(185);self.clusters.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
        self.refreshbutton=QPushButton('Ridisegna');self.refreshbutton.clicked.connect(self.refresh_map);lh.addWidget(self.refreshbutton)
        fit=QPushButton('Centra');fit.clicked.connect(lambda:self.map.reset_view());lh.addWidget(fit);controls.addLayout(lh)
        self.expand_map=QPushButton('Espandi mappa');self.expand_map.setCheckable(True)
        self.expand_map.setToolTip('Nascondi il pannello dei dettagli per usare tutta la larghezza della scheda.')
        self.expand_map.toggled.connect(self.toggle_map_details);lh.addWidget(self.expand_map)
        options=QHBoxLayout();self.map_scope=QComboBox();self.map_scope.addItem('Documenti','documents');self.map_scope.addItem('Passaggi di testo','passages')
        self.map_scope.setToolTip('Documenti: quadro generale. Passaggi: distingue i temi dei testi lunghi. Nei corpora con audit, i passaggi esclusi dalla scoperta non compaiono nella mappa.')
        self.map_scope.currentIndexChanged.connect(self.refresh_map);options.addWidget(self.map_scope)
        options.addStretch()
        self.history_toggle=QPushButton('Nascondi andamento');self.history_toggle.setCheckable(True);self.history_toggle.setChecked(True)
        self.history_toggle.toggled.connect(self.toggle_history);options.addWidget(self.history_toggle)
        self.fullscreen_button=QPushButton('Schermo intero');self.fullscreen_button.setToolTip('Solo mappa, ricerca e risultati. F11 o Esc per uscire.')
        self.fullscreen_button.clicked.connect(self.toggle_fullscreen);options.addWidget(self.fullscreen_button)
        controls.addLayout(options);ll.addWidget(self.map_controls)
        self._focus_state=None
        self.fullscreen_shortcut=QShortcut(QKeySequence('F11'),self);self.fullscreen_shortcut.activated.connect(self.toggle_fullscreen)
        self.escape_shortcut=QShortcut(QKeySequence('Esc'),self);self.escape_shortcut.setEnabled(False);self.escape_shortcut.activated.connect(self.exit_fullscreen)
        self.map=Map();self.map.chosen.connect(self.select_point)
        self.map_legend_splitter=QSplitter(Qt.Orientation.Vertical);self.map_legend_splitter.addWidget(self.map)
        self.legend_panel=QWidget();legend_layout=QVBoxLayout(self.legend_panel);legend_layout.setContentsMargins(0,0,0,0);legend_layout.setSpacing(4)
        self.map_legend_splitter.addWidget(self.legend_panel);self.map_legend_splitter.setChildrenCollapsible(False)
        self.map_legend_splitter.setStretchFactor(0,1);self.map_legend_splitter.setStretchFactor(1,0)
        self.map_legend_splitter.setSizes([400,130]);ll.addWidget(self.map_legend_splitter,1)
        legend_bar=QHBoxLayout();legend_bar.addWidget(QLabel('GRUPPI'));legend_bar.addStretch()
        self.legend_toggle=QPushButton('Nascondi temi');self.legend_toggle.setCheckable(True);self.legend_toggle.setChecked(True)
        self.legend_toggle.setStyleSheet('font-size:12px;padding:3px 9px;border-bottom-width:2px;')
        self.legend_toggle.toggled.connect(self.toggle_legend);legend_bar.addWidget(self.legend_toggle)
        self.topic_filter=QLineEdit();self.topic_filter.setPlaceholderText('Cerca fra i temi scoperti')
        self.topic_filter.setVisible(self.stable_camera);self.topic_filter.textChanged.connect(self.filter_topic_legend)
        legend_bar.addWidget(self.topic_filter)
        self.showallbutton=QPushButton('Mostra tutti');self.showallbutton.setStyleSheet('font-family:"DejaVu Sans";font-size:12px;padding:3px 10px;border-bottom-width:2px;')
        self.showallbutton.setEnabled(False);self.showallbutton.clicked.connect(self.show_all_groups);legend_bar.addWidget(self.showallbutton);legend_layout.addLayout(legend_bar)
        self.legend=QListWidget();self.legend.setAccessibleName('Gruppi della mappa');self.legend.setIconSize(QSize(16,16));self.legend.setMinimumHeight(42)
        self.legend.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff);self.legend.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.legend.currentItemChanged.connect(self.legend_selected);legend_layout.addWidget(self.legend,1)
        self.group_rows=[];self.search_rows=[];self._mapped_scope=None;self._mapped_run=None
        splitter.addWidget(left)
        right=QFrame();right.setObjectName('panel');rl=QVBoxLayout(right);title=QLabel('DOCUMENTI E RICERCA');title.setObjectName('section');rl.addWidget(title)

        self.query=QLineEdit();self.query.setPlaceholderText('Es. salario minimo' if self.is_camera else 'Es. come trovare idee per una startup');self.query.returnPressed.connect(self.search);rl.addWidget(self.query)
        searchbar=QHBoxLayout();self.searchbutton=QPushButton('Cerca');self.searchbutton.clicked.connect(self.search);searchbar.addWidget(self.searchbutton)
        self.clearbutton=QPushButton('Azzera ricerca');self.clearbutton.clicked.connect(self.clear_search);searchbar.addWidget(self.clearbutton);rl.addLayout(searchbar)
        self.timeline_button=QPushButton('Andamento del tema nel tempo');self.timeline_button.setEnabled(False)
        self.timeline_button.setToolTip('Dopo una ricerca: passaggi affini per mese o anno, copertura dell’archivio e fonti.');self.timeline_button.clicked.connect(self.show_timeline);rl.addWidget(self.timeline_button)
        self.results=QTextBrowser();self.results.setOpenLinks(False);self.results.anchorClicked.connect(self.open_result);self.results.clear();rl.addWidget(self.results,1)
        self.tabs.addTab(right,'Biblioteca e ricerca')
        self.map_inspector=QFrame();self.map_inspector.setObjectName('panel');inspector=QVBoxLayout(self.map_inspector)
        self.inspector_tabs=QTabWidget();inspector.addWidget(self.inspector_tabs,1)
        self.map_search_results=QTextBrowser();self.map_search_results.setOpenLinks(False);self.map_search_results.anchorClicked.connect(self.open_result)
        self.map_search_results.clear()
        self.map_results=QTextBrowser();self.map_results.setOpenLinks(False);self.map_results.anchorClicked.connect(self.open_result)
        self.inspector_tabs.addTab(self.map_search_results,'Risultati');self.inspector_tabs.addTab(self.map_results,'Dettagli')
        splitter.addWidget(self.map_inspector)
        splitter.setStretchFactor(0,4);splitter.setStretchFactor(1,1);splitter.setSizes([1050,330])
        self.map_page=QWidget();map_layout=QVBoxLayout(self.map_page);map_layout.setContentsMargins(0,0,0,0)
        map_search=QHBoxLayout();self.map_query=QLineEdit();self.map_query.setPlaceholderText(self.query.placeholderText())
        self.map_query.setAccessibleName('Cerca un tema nella mappa');self.map_query.returnPressed.connect(self.search)
        self.map_query.textChanged.connect(self.query.setText);self.query.textChanged.connect(self.map_query.setText)
        map_search.addWidget(self.map_query,1)
        self.map_searchbutton=QPushButton('Cerca');self.map_searchbutton.clicked.connect(self.search);map_search.addWidget(self.map_searchbutton)
        self.map_clearbutton=QPushButton('Azzera ricerca');self.map_clearbutton.clicked.connect(self.clear_search);map_search.addWidget(self.map_clearbutton)
        map_layout.addLayout(map_search)
        from history_gui import HistoryPanel
        self.history=HistoryPanel(COLORS);self.history.group_chosen.connect(self.select_group)
        self.history.reset_requested.connect(self.clear_search);self.history.detail_requested.connect(self.show_timeline)
        self.map_vertical=QSplitter(Qt.Orientation.Vertical);self.map_vertical.addWidget(splitter);self.map_vertical.addWidget(self.history)
        self.map_vertical.setCollapsible(1,False);self.map_vertical.setStretchFactor(0,3);self.map_vertical.setStretchFactor(1,1)
        self.map_vertical.setSizes([560,210]);map_layout.addWidget(self.map_vertical,1)
        self.tabs.addTab(self.map_page,'Mappa semantica')
        self.librarylabel=QLabel('BIBLIOTECA');self.librarylabel.setObjectName('section');self.librarylabel.setWordWrap(True);rl.addWidget(self.librarylabel)
        row=QHBoxLayout();row.addStretch()
        self.settingsbutton=QPushButton('Velocità');self.settingsbutton.clicked.connect(self.performance_settings);row.addWidget(self.settingsbutton)
        self.performancebutton=QPushButton('Prestazioni e rapporto');self.performancebutton.clicked.connect(self.show_performance);row.addWidget(self.performancebutton)
        self.resetbutton=QPushButton('Elimina tutti gli embedding');self.resetbutton.clicked.connect(self.reset_embeddings)
        self.resetbutton.setToolTip('Elimina, dopo conferma, tutti gli embedding della biblioteca. Conserva documenti e copie locali e li rimette in coda.');row.addWidget(self.resetbutton)
        self.removebutton=QPushButton('Rimuovi dalla coda');self.removebutton.clicked.connect(self.remove_queued);row.addWidget(self.removebutton);rl.addLayout(row)
        self.table=QTableWidget(0,4);self.table.setHorizontalHeaderLabels(['Documento','Stato','Parti / pagine','Dettagli'])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows);self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setColumnWidth(1,195);self.table.setColumnWidth(2,140);self.table.horizontalHeader().setSectionResizeMode(0,QHeaderView.ResizeMode.Stretch);self.table.horizontalHeader().setSectionResizeMode(3,QHeaderView.ResizeMode.Stretch)
        self.table.setMinimumHeight(180);self.table.verticalHeader().hide();self.table.cellDoubleClicked.connect(self.open_row);rl.addWidget(self.table,1)
        self.table.itemSelectionChanged.connect(self.select_row)

        self.timer=QTimer(self);self.timer.timeout.connect(self.tick);self.timer.start(500)
        if self.is_camera or self.stable_camera:
            self.map_scope.blockSignals(True);self.map_scope.setCurrentIndex(1);self.map_scope.blockSignals(False)
        self.refresh_table();QTimer.singleShot(0,self.refresh_map)
    def tab_changed(self,index):
        if getattr(self,'_focus_state',None) is not None:return
        library=index==0
        for widget in (self.index_controls,self.backendlabel,self.progress,self.status):widget.setVisible(library)
        self.stats_panel.setVisible(library and self.stats_toggle.isChecked())
    def toggle_map_details(self,expanded):
        self.map_inspector.setVisible(not expanded)
        self.expand_map.setText('Mostra dettagli' if expanded else 'Espandi mappa')
        QTimer.singleShot(0,self.map.reset_view)
    def show_map_details(self,results=False):
        if self._focus_state is not None:
            self.map_inspector.show();self.inspector_tabs.setCurrentIndex(0);return
        self.expand_map.setChecked(False)
        if not self.map_splitter.sizes()[1]:self.map_splitter.setSizes([1050,330])
        self.inspector_tabs.setCurrentIndex(0 if results or self._focus_state is not None else 1)
    def toggle_history(self,visible):
        self.map._navigated=True
        if not visible:self._history_sizes=self.map_vertical.sizes()
        self.history.setVisible(visible)
        self.history_toggle.setText('Nascondi andamento' if visible else 'Mostra andamento')
        if visible:self.map_vertical.setSizes(getattr(self,'_history_sizes',[560,210]))

    def toggle_fullscreen(self):
        if self._focus_state is not None:
            self.exit_fullscreen();return
        widgets=(self.header,self.separator,self.index_controls,self.stats_panel,self.backendlabel,
            self.status,self.progress,self.tabs.tabBar(),self.map_controls,self.legend_panel,
            self.history,self.inspector_tabs.tabBar(),self.map_inspector)
        layouts=(self.centralWidget().layout(),self.map_frame.layout(),self.map_inspector.layout())
        self._focus_state=dict(widgets=[(w,w.isHidden()) for w in widgets],
            margins=[(l,l.contentsMargins(),l.spacing()) for l in layouts],
            tab=self.tabs.currentIndex(),inspector=self.inspector_tabs.currentIndex(),
            state=self.windowState(),geometry=self.saveGeometry(),
            splitters=[(w,w.sizes()) for w in (self.map_splitter,self.map_vertical,self.map_legend_splitter)],
            transform=self.map.transform(),center=self.map.mapToScene(self.map.viewport().rect().center()),
            navigated=self.map._navigated)
        self.map._navigated=True
        self.tabs.setCurrentWidget(self.map_page)
        for widget in widgets:widget.hide()
        for layout in layouts:layout.setContentsMargins(0,0,0,0);layout.setSpacing(0)
        self.map_inspector.show();self.inspector_tabs.setCurrentIndex(0)
        self.map_splitter.setSizes([1050,330]);self.map_query.setFocus()
        self.escape_shortcut.setEnabled(True);self.showFullScreen()

    def exit_fullscreen(self):
        state=self._focus_state
        if state is None:return
        transform=self.map.transform();center=self.map.mapToScene(self.map.viewport().rect().center())
        self.map._navigated=True
        # Keep the guard active while restoring tabs: tab_changed must not
        # override the user's panel choices or reset the current viewport.
        self.setWindowState(state['state']);self.restoreGeometry(state['geometry'])
        self.tabs.setCurrentIndex(state['tab']);self.inspector_tabs.setCurrentIndex(state['inspector'])
        for widget,hidden in state['widgets']:widget.setHidden(hidden)
        for layout,margins,spacing in state['margins']:layout.setContentsMargins(margins);layout.setSpacing(spacing)
        for splitter,sizes in state['splitters']:splitter.setSizes(sizes)
        self.map.setTransform(transform);self.map.centerOn(center);self.map._navigated=True
        self.escape_shortcut.setEnabled(False);self._focus_state=None

    def update_history(self):
        if not hasattr(self,'history'):return
        group=self.map.active_group
        if group is not None or self.timeline_data is None:
            self.history.show_overview(self.history_catalog,self.map.items_data,self.map_scope.currentData(),group)
        else:self.history.show_query(self.timeline_data)
    def get_engine(self):
        backend='vulkan' if self.backend.currentIndex()==0 else 'cpu'
        config=self.settings[backend]
        text_only=all(Path(doc['source']).suffix.lower() in {'.txt','.md'} for doc in self.archive.documents())
        if self.engine and (self.engine.backend!=backend or getattr(self.engine,'workers',1)!=config['workers'] or
                            getattr(self.engine,'per_worker_batch',4)!=config['batch_size'] or self.engine.text_only!=text_only):
            self.engine.close();self.engine=None
        if not self.engine:self.engine=EnginePool(self.archive.data,backend,text_only=text_only,**config)
        return self.engine
    def save_settings(self):
        (self.archive.data/'preferenze.json').write_text(json.dumps(self.settings,indent=2))
    def performance_settings(self):
        if self.task:return
        backend='vulkan' if self.backend.currentIndex()==0 else 'cpu';config=self.settings[backend]
        dialog=QDialog(self);dialog.setWindowTitle('Velocità · '+backend.upper());dialog.resize(540,260)
        box=QVBoxLayout(dialog)
        text=QLabel('Più passaggi vengono calcolati insieme. Due worker usano due processi del modello e più memoria; il vantaggio dipende dal dispositivo.\n\nLa prova confronta 7 configurazioni su 16 passaggi reali, senza aggiungere dati alla biblioteca, e applica la più veloce misurata.');text.setWordWrap(True);box.addWidget(text)
        row=QHBoxLayout();row.addWidget(QLabel('Worker'));workers=QComboBox();workers.addItems(['1','2']);workers.setCurrentText(str(config['workers']));row.addWidget(workers)
        row.addWidget(QLabel('Passaggi per worker'));batch=QComboBox();batch.addItems(['1','2','4','8']);batch.setCurrentText(str(config['batch_size']));row.addWidget(batch);box.addLayout(row)
        save=QPushButton('Applica');box.addWidget(save)
        def apply():
            self.settings[backend]=dict(workers=int(workers.currentText()),batch_size=int(batch.currentText()));self.save_settings();dialog.accept()
            if self.engine:self.engine.close();self.engine=None
            self.status.setText(f"Configurazione {backend}: {workers.currentText()} worker, batch {batch.currentText()}.")
        save.clicked.connect(apply)
        measure=QPushButton('Confronta e scegli automaticamente');box.addWidget(measure)
        def compare():dialog.accept();QTimer.singleShot(0,self.benchmark)
        measure.clicked.connect(compare);dialog.exec()
    def benchmark(self):
        if self.task:return
        from performance import benchmark
        backend='vulkan' if self.backend.currentIndex()==0 else 'cpu'
        if self.engine:self.engine.close();self.engine=None
        self.status.setText('Confronto prestazioni in corso…')
        def done(result):
            self.settings[backend]=result['selected'];self.save_settings()
            parts=['<h3>Confronto prestazioni · '+backend.upper()+'</h3>']
            for row in result['rows']:
                parts.append(f'<p>{row["workers"]} worker · batch {row["batch_size"]}: <b>{row["embeddings_per_second"]:.2f} embedding/s</b></p>')
            parts.append('<p>'+html.escape(result['note'])+'</p>');self.results.setHtml(''.join(parts))
            chosen=result['selected'];self.status.setText(f"Applicati {chosen['workers']} worker, batch {chosen['batch_size']}. Velocità misurata: {result['speedup_vs_single']:.2f}× rispetto a 1 worker e 1 passaggio.")
        self.execute(lambda stop,notify:benchmark(self.archive.data,backend,stop,notify),done)
    def execute(self,fn,result):
        if self.task:return
        self.task=Task(fn);self.task.result.connect(result);self.task.failure.connect(self.failed);self.task.paused.connect(self.paused);self.task.progress.connect(self.on_progress)
        self.task.finished.connect(self.finished);self.busy(True);self.task.start()
    def busy(self,state):
        for w in [self.addbutton,self.corpusbutton,self.settingsbutton,self.startbutton,self.searchbutton,self.clearbutton,self.map_searchbutton,self.map_clearbutton,self.refreshbutton,self.resetbutton,self.removebutton,self.backend,self.clusters,self.map_scope,self.gpubutton,self.legend,self.history]:w.setEnabled(not state)
        if self.stable_camera:self.clusters.setEnabled(False)
        self.showallbutton.setEnabled(not state and self.map.active_group is not None)
        self.stopbutton.setEnabled(state)
        if self.timeline_button:self.timeline_button.setEnabled(not state and self.timeline_data is not None)
    def failed(self,message):
        lines=[line.strip() for line in message.splitlines() if line.strip()]
        summary=(lines[-1] if lines else 'Errore sconosciuto')[:220]
        if self.engine and self.engine.backend=='vulkan' and not self.engine.info.get('gpu_verified'):
            self.backendlabel.setText('GPU non verificata: '+summary)
        self.metrics['eta'].setText('—')
        self.status.setText('Elaborazione interrotta. '+summary)
        self.results.append('<p style="color:#98624d"><b>Dettagli dell’interruzione</b><br>'+html.escape(message).replace('\n','<br>')+'</p>')
        if not self.stats.get('pages_processed',0):self.need_map=False
    def paused(self,message):
        self.metrics['eta'].setText('—')
        self.status.setText('Operazione sospesa. Gli embedding salvati sono consultabili in ricerca e nella scheda Mappa semantica.')
    def finished(self):
        self.task.deleteLater();self.task=None;self.busy(False);self.started=None;self.refresh_table()
        if self.closing:self.close();return
        if getattr(self,'need_map',False):self.need_map=False;QTimer.singleShot(0,self.refresh_map)
    def cancel(self):
        if self.task:self.task.stop.set();self.status.setText('Sospensione richiesta. Conservo le pagine completate…');self.stopbutton.setEnabled(False)
    def on_progress(self,value):
        if isinstance(value,str):self.status.setText(value);return
        self.stats=value;self.stats_time=time.monotonic();self.status.setText(value.get('stage',''));self.tick()
        total=value.get('total',0);done=value.get('finished',0)
        self.metrics['done'].setText(f'{done} / {total}');self.metrics['remaining'].setText(str(max(0,total-done)))
        fraction=value.get('current_page',0)/max(1,value.get('current_pages',1))
        self.progress.setValue(round(100*(done+fraction)/total) if total else 0)
    def tick(self):
        if self.started:self.metrics['elapsed'].setText(duration(time.monotonic()-self.started))
        eta=self.stats.get('eta')
        if eta is not None:eta=max(0,eta-(time.monotonic()-getattr(self,'stats_time',time.monotonic())))
        self.metrics['eta'].setText(duration(eta) if self.started else self.metrics['eta'].text())
        if self.engine:
            info=self.engine.info;resources=self.engine.resources()
            if info.get('ready'):
                gpu=info.get('gpu_verified');layers=info.get('layers')
                text=('GPU integrata verificata · ' if gpu and info.get('integrated_gpu') else 'GPU verificata · ' if gpu else 'GPU in verifica · ' if info.get('backend')=='Vulkan' else 'CPU · ')+info.get('device','')
                if layers:text+=f" · {layers[0]}/{layers[1]} strati su Vulkan"
                text+=' · solo testo' if info.get('text_only') else ' · testo e immagini'
                text+=f" · {info.get('workers',1)} worker · batch {info.get('batch_size',1)}"
                if 'gpu_device_busy_percent' in resources:text+=f" · GPU dispositivo: {resources['gpu_device_busy_percent']}%"
                if 'gpu_vram_mib' in resources:text+=f" · VRAM {resources['gpu_vram_mib']:.0f} MiB · GTT {resources.get('gpu_gtt_mib',0):.0f} MiB"
                self.backendlabel.setText(text)
            if self.started:
                stats=self.engine.stats;elapsed=time.monotonic()-self.started
                processing=max(0.,elapsed-stats['load_seconds']);pages=self.stats.get('pages_processed',0)
                vectors=stats['text_embeddings']+stats['image_embeddings']
                self.perf['pages'].setText(str(pages));self.perf['vectors'].setText(str(vectors))
                self.perf['pps'].setText(f'{pages/processing:.3f}' if processing else '—')
                self.perf['eps'].setText(f'{vectors/processing:.2f}' if processing else '—')
                self.perf['load'].setText(f"{stats['load_seconds']:.2f} s")
            if 'ram_mib' in resources:self.perf['ram'].setText(f"{resources['ram_mib']:.0f} MiB")

    def index(self):
        if self.task:return
        if not self.archive.pending() and not self.stable_camera:self.status.setText('Nessun nuovo documento da elaborare. Aggiungi un file.');return
        self.invalidate_timeline()
        self.stats_toggle.setChecked(True)
        self.started=time.monotonic();self.stats={};self.need_map=True;engine=self.get_engine()
        def work(stop,notify):
            if self.stable_camera and not self.is_camera:
                from essay_dataset import prepare
                prepare(self.archive)
            result=self.archive.index(engine,stop,notify)
            if self.stable_camera and not result['errors']:
                from topic_discovery import discover
                engine.close()
                root=self.archive.data.parent if self.is_camera else self.archive.data
                result['discovery']=discover(root,stop,notify)
                if self.is_camera:
                    from camera_quality import export
                    export(root)
                else:
                    from essay_dataset import export
                    export(self.archive)
            return result
        self.execute(work,self.index_done)
    def index_done(self,result):
        self.status.setText(f"Terminato: {result['completed']} documenti completati, {result['duplicates']} duplicati ignorati, {result['errors']} errori. Tempo: {duration(result['seconds'])}.")
        self.metrics['elapsed'].setText(duration(result['elapsed_seconds']));self.metrics['eta'].setText('00:00:00')
        self.perf['pages'].setText(str(result['pages_processed']))
        self.perf['pps'].setText(f"{result['pages_per_second']:.3f}")
        self.perf['vectors'].setText(str(result['text_embeddings']+result['image_embeddings']))
        self.perf['eps'].setText(f"{result['embeddings_per_second']:.2f}")
        self.perf['load'].setText(f"{result['load_seconds']:.2f} s")
        info=result.get('engine',{})
        if info:
            self.backend.setCurrentIndex(1 if result.get('backend_requested')=='cpu' else 0)
            self.backendlabel.setText(f"Sessione {info.get('backend','')} · {info.get('device','')} · {info.get('workers',1)} worker · batch {info.get('batch_size',1)}"+(' · solo testo' if info.get('text_only') else ' · testo e immagini'))
            ram=result.get('resources',{}).get('ram_mib')
            if ram is not None:self.perf['ram'].setText(f'{ram:.0f} MiB')
    def verify_gpu(self):
        if self.task:return
        self.backend.setCurrentIndex(0);engine=self.get_engine()
        for value in self.perf.values():value.setText('—')
        def work(stop,notify):
            engine.close();engine.reset_stats();engine.start(stop,notify)
            result=dict(engine=engine.info,resources=engine.resources(),load_seconds=engine.stats['load_seconds'])
            (self.archive.data/'verifica-gpu.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
            return result
        def done(result):
            self.tick();self.perf['load'].setText(f"{result['load_seconds']:.2f} s")
            self.status.setText('Verifica GPU riuscita: inferenza reale '+('del testo' if result['engine'].get('text_only') else 'di testo e immagine')+'. Rapporto salvato in verifica-gpu.json.')
        self.execute(work,done)
    def show_performance(self):
        path=self.archive.data/'ultimo-rapporto.json'
        if self.started and self.engine:
            elapsed=time.monotonic()-self.started;stats=dict(self.engine.stats);processing=max(0.,elapsed-stats['load_seconds'])
            pages=self.stats.get('pages_processed',0);vectors=stats['text_embeddings']+stats['image_embeddings']
            report=dict(status='in corso',elapsed_seconds=elapsed,processing_seconds=processing,
                        pages_processed=pages,pages_per_second=pages/processing if processing else 0.,
                        embeddings_per_second=vectors/processing if processing else 0.,engine=self.engine.info,
                        resources=self.engine.resources(),documents=list(getattr(self.archive,'run_documents',[])),**stats,**getattr(self.archive,'timings',{}))
        elif path.exists():report=json.loads(path.read_text())
        else:QMessageBox.information(self,'Prestazioni','Il rapporto sarà disponibile dopo il primo calcolo, anche se sospeso.');return
        dialog=QDialog(self);dialog.setWindowTitle('Prestazioni · '+report.get('status',''));dialog.resize(920,680)
        box=QVBoxLayout(dialog);view=QTextBrowser();box.addWidget(view)
        info=report.get('engine',{});device=html.escape(info.get('backend','')+' · '+info.get('device',''))
        rows=[('Dispositivo',device),('GPU verificata ('+('testo' if info.get('text_only') else 'testo e immagine')+')','Sì' if info.get('gpu_verified') else 'No / CPU'),
              ('Worker / passaggi per worker',f"{info.get('workers',1)} / {info.get('batch_size',1)}"),
              ('Tempo totale',duration(report['elapsed_seconds'])),('Tempo di elaborazione (escluso caricamento)',f"{report['processing_seconds']:.2f} s"),
              ('Verifica pesi, caricamento e prova GPU',f"{report['load_seconds']:.2f} s"),
              ('Parti / pagine completate in questa sessione',str(report['pages_processed'])),
              ('Parti / pagine al secondo',f"{report['pages_per_second']:.4f}"),
              ('Embedding testuali',str(report['text_embeddings'])),('Embedding visivi',str(report['image_embeddings'])),
              ('Embedding / secondo (intera elaborazione)',f"{report['embeddings_per_second']:.3f}"),
              ('Token elaborati, inclusi token visivi',str(report['input_tokens'])),
              ('Tempo di inferenza (somma dei worker)',f"{report['embedding_seconds']:.2f} s"),
              ('Tempo reale nei batch (worker sovrapposti)',f"{report.get('batch_wall_seconds',0):.2f} s"),
              ('Token / secondo (intera elaborazione)',f"{report['input_tokens']/max(report['processing_seconds'],.001):.1f}"),
              ('Tempo tokenizzazione',f"{report['tokenizer_seconds']:.2f} s"),
              ('Preparazione, copia e lettura metadati',f"{report.get('preparation_seconds',0):.2f} s"),
              ('Estrazione layout e rasterizzazione',f"{report.get('extraction_seconds',0):.2f} s"),
              ('Salvataggio embedding nel database',f"{report.get('storage_seconds',0):.2f} s")]
        resources=report.get('resources',{})
        for key,label in [('ram_peak_mib','Picco RAM del modello'),('gpu_vram_mib','VRAM del processo'),('gpu_gtt_mib','Memoria GPU condivisa (GTT)')]:
            if key in resources:rows.append((label,f'{resources[key]:.1f} MiB'))
        content='<h2>Prestazioni della sessione</h2><table cellspacing="8">'+''.join(f'<tr><td>{k}</td><td><b>{v}</b></td></tr>' for k,v in rows)+'</table>'
        content+='<p>Una parte TXT/MD corrisponde a un passaggio; il rapporto conserva il limite applicato in quella sessione. Per PDF si contano le pagine. I tassi includono lettura, inferenza e salvataggio, escluso il caricamento iniziale. Contano solo le parti nuove. Gli embedding possono includere tentativi interrotti. Con due worker la somma dei tempi di inferenza può superare il tempo reale trascorso.</p>'
        content+='<h3>Documenti</h3><table cellspacing="8"><tr><th>Nome</th><th>Stato</th><th>Parti / pagine nuove</th><th>Secondi</th><th>Parti / pagine al sec.</th></tr>'
        for doc in report.get('documents',[]):
            content+=f'<tr><td>{html.escape(doc["name"])}</td><td>{html.escape(doc["status"])}</td><td>{doc["new_pages"]}</td><td>{doc["seconds"]:.2f}</td><td>{doc["pages_per_second"]:.4f}</td></tr>'
        content+='</table><p>Rapporti JSON conservati in '+html.escape(str(self.archive.data/'rapporti'))+'</p>'
        view.setHtml(content)
        save=QPushButton('Esporta rapporto JSON')
        def export():
            name,_=QFileDialog.getSaveFileName(dialog,'Esporta rapporto',str(Path.home()/'GemmaDesk-prestazioni.json'),'JSON (*.json)')
            if name:Path(name).write_text(json.dumps(report,ensure_ascii=False,indent=2))
        save.clicked.connect(export);box.addWidget(save);dialog.exec()
    def filter_topic_legend(self,text):
        terms=text.casefold().split()
        if not hasattr(self,'legend'):return
        for index in range(self.legend.count()):
            item=self.legend.item(index);value=item.text().casefold()
            item.setHidden(not all(term in value for term in terms))
    def toggle_legend(self,visible):
        sizes=self.map_legend_splitter.sizes();total=sum(sizes)
        if not visible:self._legend_height=sizes[1]
        self.legend.setVisible(visible);self.topic_filter.setVisible(visible and self.stable_camera)
        height=max(110,getattr(self,'_legend_height',130)) if visible else 38
        self.map_legend_splitter.setSizes([max(120,total-height),height])
        count=len({p['cluster'] for p in self.map.items_data if p['cluster']>=0})
        self.legend_toggle.setText(('Nascondi' if visible else 'Mostra')+f' temi ({count})')
    def refresh_map(self):
        if self.task:return
        k=self.clusters.value();scope=self.map_scope.currentData();self.execute(lambda stop,notify:self.archive.map(stop,k,notify,scope),self.map_done)
    def map_done(self,items):
        from timeline import catalog
        self.history_catalog=catalog(self.archive)
        had_group=self.map.active_group is not None
        new_run=items[0].get('topic_run') if items else None
        group=self.map.active_group if self._mapped_scope==self.map_scope.currentData() and self._mapped_run==new_run else None
        self._mapped_run=new_run
        self._mapped_scope=self.map_scope.currentData()
        if self.stable_camera:
            from topic_discovery import catalog as topic_catalog
            with self.archive.connect() as db:known,_,report=topic_catalog(db)
            self.annual_topics=report.get('mode')=='annual-aligned-v1'
            count=sum(group>=0 for group in known)
            self.clusters.setValue(0);self.clusters.setSpecialValueText(f'{count} temi scoperti' if count else 'Scoperta in attesa')
        self.map.active_group=group
        self.map.set_data(items)
        fraction=items[0].get('explained') if items else None
        variance=f' · Varianza mostrata: {fraction:.0%}' if fraction is not None else ''
        passages=self.map_scope.currentData()=='passages';unit='passaggi' if passages else 'documenti'
        partials=len({item['id'] for item in items if item.get('partial')})
        partial_caption=('\n'+(f'{partials} documenti parziali' if partials!=1 else '1 documento parziale')+': bordo tratteggiato, solo parti salvate.') if partials else ''
        method=items[0].get('projection','PCA') if items else 'PCA'
        note=' · vicinanze locali, distanze fra gruppi non quantitative' if method in ('t-SNE','UMAP') else ' · distanze approssimative'
        if self.is_camera and not passages:note+='\nUna seduta può trattare più temi: usa Passaggi di testo per distinguerli.'
        grouping_hint='Temi appresi dal corpus · ' if self.stable_camera else 'Più gruppi = più dettaglio · '
        self.map.setToolTip('')
        groups={}
        for item in items:groups.setdefault(item['cluster'],[]).append(item)
        counts={ident:len(members) for ident,members in groups.items()}
        QTimer.singleShot(0,self.map.reset_view)
        topics={i['cluster']:i.get('topics',[]) for i in items}
        self.legend.blockSignals(True);self.legend.clear()
        def legend_order(entry):
            ident,members=entry;names=topics[ident]
            if self.stable_camera:return (ident<0,-sum(item.get('topic_words',0) for item in members),ident)
            return (ident<0 or any(name.startswith('Temi diversi') for name in names),ident<0,ident)
        for ident,members in sorted(groups.items(),key=legend_order):
            documents=len({item['id'] for item in members})
            count=f'{documents} documenti' if not passages else f'{len(members)} passaggi · {documents} documenti'
            text=group_label(ident)+' · '+(' · '.join(topics[ident]) or 'Temi non disponibili')+f'  ({count})'
            swatch=QPixmap(16,16);swatch.fill(Qt.GlobalColor.transparent)
            painter=QPainter(swatch);painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(Qt.PenStyle.NoPen);painter.setBrush(QColor(group_color(ident)));painter.drawEllipse(1,1,14,14);painter.end()
            entry=QListWidgetItem(QIcon(swatch),text);entry.setData(Qt.ItemDataRole.UserRole,ident)
            entry.setToolTip(text+'\nSeleziona per evidenziare i punti e vedere tutti i documenti del gruppo.')
            self.legend.addItem(entry)
            if ident==group:self.legend.setCurrentItem(entry)
        if not groups:
            entry=QListWidgetItem('Nessun passaggio testuale indicizzato' if passages else 'Nessun documento indicizzato')
            entry.setFlags(Qt.ItemFlag.NoItemFlags);self.legend.addItem(entry)
        self.legend_toggle.setText(('Nascondi' if self.legend_toggle.isChecked() else 'Mostra')+f' temi ({sum(g>=0 for g in groups)})')
        self.legend.blockSignals(False)
        self.filter_topic_legend(self.topic_filter.text())
        score=items[0].get('group_score') if items else None
        details='Gruppi gerarchici negli embedding a 768 dimensioni. A parità di biblioteca e vista, ogni gruppo aggiuntivo divide un solo gruppo; gli altri conservano composizione, numero, colore ed etichette. Il sottogruppo più grande conserva il numero del genitore. Auto sceglie un livello dello stesso albero; con separazione debole resta un gruppo. Fino a tre espressioni distintive rispetto all’intera raccolta e al ramo fratello alla nascita del sottogruppo, bilanciate per documento. Link alle traduzioni e ringraziamenti sono esclusi solo dai nomi, non dalla ricerca. Etichette nella lingua dei testi, senza modello generativo. Immagini senza testo: temi non disponibili.'
        if self.is_camera:
            details='Temi ricavati dal contenuto dei passaggi, dagli embedding salvati e dagli argomenti verificati dei resoconti. Formule procedurali e nomi degli oratori vengono esclusi dalla rappresentazione tematica. Grigio: passaggi brevi o procedurali, ancora consultabili e inclusi nei totali. I nomi sono espressioni ricorrenti o titoli prevalenti del dibattito. Gruppi gerarchici fino a 24; ogni gruppo aggiuntivo divide un solo gruppo, mantenendo le posizioni. La proiezione t-SNE privilegia le vicinanze locali: distanze fra gruppi e dimensione delle nuvole non misurano la differenza o l’importanza dei temi. Gruppi ed etichette sono indicativi: argomenti correlati possono sovrapporsi.'
        if score is not None:details+=f' Silhouette coseno: {score:.3f}, misurata su {items[0]["group_sample"]} punti; valori più alti indicano gruppi meglio separati, non categorie certificate.'
        if self.stable_camera:
            details='Temi scoperti con BERTopic dagli embedding dei passaggi, senza categorie iniziali. Numero e nomi dipendono dal corpus; lo stesso modello copre tutti gli anni. Una seconda analisi cerca temi nei residui; il recupero richiede accordo tra contenuto e somiglianza. Le formule e i tabulati riconosciuti sono esclusi dalle quote tematiche, con audit. Grigio: incerti, esclusi o in attesa. Grafico annuale: quota delle parole orali, scritti separati negli export. Serie provvisorie finché indicizzazione e scoperta sono incomplete. UMAP: vicinanze locali; distanze fra gruppi non quantitative. Proiezione salvata su disco. Esempi, qualità e CSV: cartella temi. I numeri dei gruppi si riferiscono alla versione del modello.'
            if not self.is_camera:
                details='Temi scoperti dai testi inglesi con BERTopic, senza categorie iniziali; stesso modello per tutti gli anni e le viste. Passaggi senza sovrapposizioni, date di pubblicazione e quote di parole. Incerti espliciti, audit e CSV nella cartella temi. UMAP mostra vicinanze locali, non distanze quantitative fra temi.'
        if getattr(self,'annual_topics',False):
            details='Temi scoperti separatamente per anno, poi armonizzati con embedding e lessico. Ogni anno può introdurre temi nuovi. Le corrispondenze sono in temi/temi-annuali.json; etichette e collegamenti richiedono revisione. I numeri si riferiscono alla versione del modello. UMAP conserva la geometria degli embedding.'
        examples={i['cluster']:i.get('topic_examples',[]) for i in items}
        for ident,names in sorted(examples.items()):
            if names:details+='\n'+group_label(ident)+' — esempi: '+'; '.join(names)
        self.legend.setToolTip(details)
        if group is not None and group in groups:self.select_group(group)
        elif had_group:self.show_all_groups()
        else:self.group_rows=[];self.showallbutton.setEnabled(False);self.map_results.clear()
        self.update_history()
        if self.status.text().startswith('Mappa:'):self.status.setText(f'Mappa aggiornata: {len(items)} {unit}, {len(counts)} gruppi. Seleziona un gruppo nella legenda.')
    def legend_selected(self,current,previous):
        if current is not None:
            group=current.data(Qt.ItemDataRole.UserRole)
            if group is not None:self.select_group(group)
    def clear_group_selection(self):
        was_group=self.map.active_group is not None
        self.legend.blockSignals(True);self.legend.clearSelection();self.legend.setCurrentRow(-1);self.legend.blockSignals(False)
        self.map.active_group=None;self.map._style_dots();self.group_rows=[];self.showallbutton.setEnabled(False);self.map_results.clear()
        if was_group:self.map.reset_view()
        self.update_history()
    def show_all_groups(self):
        self.clear_group_selection();self.map.highlight([]);self.map.select_document(None)
        self.map.reset_view()
        if self.timeline_data:self.map.highlight([r['id'] for r in self.search_rows],[r['chunk_id'] for r in self.search_rows])
        self.map_results.setHtml('<p>Seleziona un gruppo nella legenda per vedere tutti i suoi documenti, oppure un punto per ispezionarlo.</p>')
        self.status.setText('Tutti i gruppi della mappa sono visibili.')
    def select_group(self,group):
        members=[item for item in self.map.items_data if item['cluster']==group]
        if not members:return
        self.legend.blockSignals(True)
        for index in range(self.legend.count()):
            entry=self.legend.item(index)
            if entry.data(Qt.ItemDataRole.UserRole)==group:self.legend.setCurrentItem(entry);break
        self.legend.blockSignals(False)
        self.map.highlight_group(group)
        self.table.blockSignals(True);self.table.clearSelection();self.table.blockSignals(False)
        documents={}
        for item in members:
            document=documents.setdefault(item['id'],dict(item,pages=set()))
            document['pages'].add(item.get('page',1))
        self.group_rows=sorted(documents.values(),key=lambda row:(row['name'].casefold(),row['id']))
        title=' · '.join(members[0].get('topics',[])) or 'Temi non disponibili'
        passages=members[0].get('scope')=='passages'
        count=f'{len(self.group_rows)} documenti'+(f' · {len(members)} passaggi' if passages else '')
        parts=[f'<h3>{group_label(group)} · {html.escape(title)}</h3><p>Tutti i documenti del gruppo: {count}</p>']
        if passages and self.is_camera:
            headings=Counter(item.get('topic_context','') for item in members if item.get('topic_context'))
            if headings:
                parts.append('<p><b>Argomenti ricorrenti nei resoconti</b></p><ul>')
                for heading,number in headings.most_common(5):parts.append('<li>'+html.escape(heading)+f' · {number} passaggi</li>')
                parts.append('</ul>')
        parts.append('<ol>')
        for index,row in enumerate(self.group_rows):
            row['pages']=sorted(row['pages']);row['page']=row['pages'][0]
            parts.append(f'<li style="margin-bottom:10px"><a href="groupdoc:{index}" style="color:#3e7079;font-size:14px">{html.escape(row["name"])}</a>')
            if row.get('partial'):parts.append('<br><b>'+partial_note(row)+'</b>')
            if passages:
                pages=', '.join(str(page) for page in row['pages'])
                parts.append(f'<br><span style="color:#66706e">Parti / pagine nel gruppo: {pages}</span>')
            parts.append('</li>')
        parts.append('</ol>');self.map_results.setHtml(''.join(parts));self.show_map_details()
        self.showallbutton.setEnabled(True)
        self.update_history()
        self.status.setText(f'{group_label(group)} · {count}. Clicca un titolo a destra per aprire il documento.')
    def search(self):
        if self.task:return
        if not self.query.text().strip():self.clear_search();return
        query=self.query.text().strip();self.invalidate_timeline();self.status.setText('Ricerca negli embedding…')
        from timeline import snapshot
        indexed=any(d['done']>0 for d in self.archive.documents())
        engine=self.get_engine() if indexed else None
        def work(stop,notify):
            vector=None;rows=[]
            if engine:
                engine.start(stop,notify);vector=engine.query(query,stop)
                rows=self.archive.search(vector,stop)
            notify('Andamento del tema: confronto con tutti i passaggi datati…')
            return rows,snapshot(self.archive,vector,stop,query)
        def done(result):
            rows,data=result;self.search_done(rows);self.timeline_data=data
            self.update_history();self.timeline_button.setEnabled(True)
            if not indexed:self.status.setText('Nessun embedding salvato: il grafico segnala i periodi ancora da elaborare. Premi Calcola embedding per iniziare.')
        self.execute(work,done)
    def invalidate_timeline(self):
        self.timeline_data=None
        if self.timeline_button:self.timeline_button.setEnabled(False)
        if self.timeline_window:self.timeline_window.close();self.timeline_window=None
        self.update_history()
    def show_timeline(self,period=None):
        if self.timeline_data is None:return
        from timeline_gui import TimelineDialog
        if self.timeline_window:self.timeline_window.close()
        self.timeline_window=TimelineDialog(self.timeline_data,self)
        self.timeline_window.threshold.setValue(self.history.threshold.value())
        if isinstance(period,str) and period:
            self.timeline_window.grouping.setCurrentIndex(1)
            index=next((i for i,r in enumerate(self.timeline_window.rows) if r['period']==period),None)
            if index is not None:self.timeline_window.table.selectRow(index)
        self.timeline_window.show()
    def clear_search(self):
        self.invalidate_timeline()
        self.clear_group_selection()
        self.query.clear();self.search_rows=[];self.map.highlight([])
        self.results.clear()
        self.map_search_results.setHtml(self.results.toHtml());self.update_history()
        self.status.setText('Ricerca azzerata. Tutti i documenti della mappa sono visibili.')
    def search_done(self,rows):
        self.clear_group_selection()
        self.search_rows=rows;self.map.highlight([r['id'] for r in rows],[r['chunk_id'] for r in rows] if all('chunk_id' in r for r in rows) else None);parts=[]
        for idx,row in enumerate(rows):
            label='Parte' if Path(row['path']).suffix.lower() in {'.txt','.md'} else 'Pagina'
            source=f' · <a href="source:{idx}" style="color:#3e7079">Sito originale</a>' if row.get('url') else ''
            partial='<br><b>'+partial_note(row)+'</b>' if row.get('partial') else ''
            parts.append(f'<p><a style="color:#3e7079;font-size:15px" href="result:{idx}">{html.escape(row["name"])}</a>{source}{partial}<br><span style="color:#66706e">{label} {row["page"]} · {row["kind"]} · coseno {row["score"]:.3f}</span><br>{html.escape(row["text"][:350])}</p><hr style="color:#c1c2b7">')
        content=''.join(parts) or '<p>Nessun embedding salvato nell’archivio.</p>'
        self.results.setHtml(content);self.map_search_results.setHtml(content);self.show_map_details(results=True)
        self.status.setText(f'{len(rows)} risultati. Il coseno misura similarità, non probabilità.')
    def open_result(self,url):
        if url.scheme()=='groupdoc':
            try:index=int(url.path())
            except ValueError:return
            if 0<=index<len(self.group_rows):
                row=self.group_rows[index];open_document(row['path'],row['page'])
            return
        if url.scheme() in {'result','source'}:
            try:row=self.search_rows[int(url.path())]
            except (ValueError,IndexError):return
            if url.scheme()=='result':open_document(row['path'],row['page'])
            elif QUrl(row.get('url','')).scheme() in {'http','https'}:QDesktopServices.openUrl(QUrl(row['url']))
    def select_point(self,item):
        self.clear_group_selection()
        self.status.setText(f"{item['name']} · {group_label(item['cluster'])} · "+' · '.join(item.get('topics',[])))
        details='<p><b>'+html.escape(item['name'])+'</b></p><p>'+group_label(item['cluster'])+' · '+html.escape(' · '.join(item.get('topics',[])) or 'Temi non disponibili')+'</p>'
        if item.get('topic_context'):details+='<p><b>Argomento nel resoconto</b><br>'+html.escape(item['topic_context']).replace('\n','<br>')+'</p>'
        if item.get('partial'):details+='<p><b>'+partial_note(item)+'</b></p>'
        if item.get('topic_examples'):
            details+='<p style="color:#66706e">Documenti rappresentativi del gruppo:<br>'+ '<br>'.join(html.escape(name) for name in item['topic_examples'])+'</p>'
        if item.get('chunk_id'):
            details+='<p>Passaggio / pagina '+str(item['page'])+'</p><p>'+html.escape(item.get('text','')).replace('\n','<br>')+'</p>'
        self.map_results.setHtml(details);self.show_map_details()
        for i,doc in enumerate(self.docs):
            if doc['id']==item['id']:
                self.table.blockSignals(True);self.table.selectRow(i);self.table.blockSignals(False)
                self.table.scrollToItem(self.table.item(i,0));break
    def select_row(self):
        self.clear_group_selection()
        selected=self.table.selectionModel().selectedRows()
        self.map.select_document(self.docs[selected[0].row()]['id'] if selected else None)
    def refresh_table(self):
        selected={self.docs[index.row()]['id'] for index in self.table.selectionModel().selectedRows()} if hasattr(self,'docs') else set()
        self.table.blockSignals(True)
        self.docs=self.archive.documents();self.table.setRowCount(len(self.docs));ready=0;partials=0
        self.table.clearSelection()
        labels={'queued':'In coda','processing':'In elaborazione','paused':'Da riprendere','ready':'Indicizzato','error':'Errore / riprova'}
        for i,doc in enumerate(self.docs):
            ready+=doc['status']=='ready'
            partial=doc['status']!='ready' and doc['done']>0;partials+=partial
            state=labels[doc['status']]
            if partial:state='Parziale · '+state.lower()
            for j,text in enumerate([doc['name'],state,f'{doc["done"]} / {doc["pages"] or "?"}',doc['error'] or doc['title'] or doc['source']]):
                item=QTableWidgetItem(str(text));item.setToolTip(str(text));self.table.setItem(i,j,item)
            if doc['id'] in selected:
                for j in range(4):self.table.item(i,j).setSelected(True)
        self.table.blockSignals(False)
        self.librarylabel.setText(f'BIBLIOTECA · {ready} completi · {partials} parziali · {len(self.docs)-ready-partials} da elaborare')
    def pick(self):
        directory=getattr(self,'last_directory',self.corpus if self.corpus.exists() else Path.home())
        dialog=DocumentPicker(directory,self)
        if dialog.exec()==QDialog.DialogCode.Accepted:
            self.last_directory=dialog.directory;self.add(dialog.paths)
    def add_corpus(self):
        paths=sorted(p for p in self.corpus.rglob('*') if p.is_file() and p.suffix.lower() in SUPPORTED)
        if not paths:self.status.setText('Raccolta non trovata in '+str(self.corpus));return
        self.add(paths)
    def add(self,paths):
        if not paths or self.task:return
        self.invalidate_timeline()
        self.status.setText(f'Aggiunta alla coda di {len(paths)} documenti…')
        def done(result):
            self.last_import=result
            n=len(result['added']);duplicates=result['duplicates'];rejected=result['rejected']
            message=f'{n} documenti aggiunti alla coda.'
            if duplicates:message+=f' {len(duplicates)} già presenti: ignorati, non saranno rielaborati.'
            if rejected:message+=f' {len(rejected)} file non aggiunti: vedi dettagli.'
            if n:message+=' Premi Calcola embedding per iniziare.'
            self.status.setText(message)
            if duplicates or rejected:
                self.map.highlight([]);self.search_rows=[];self.map_search_results.clear()
                states={'ready':'già indicizzato','queued':'già in coda','paused':'da riprendere','processing':'in elaborazione','error':'già presente, da riprovare'}
                details='<h3>Controllo documenti</h3><p>'+html.escape(message)+'</p>'
                for item in duplicates:
                    other=item['existing']
                    details+='<p><b>Già presente: '+html.escape(item['name'])+'</b><br>Identico a '+html.escape(other['name'])+' ('+states.get(other['status'],other['status'])+'). Nessuna nuova elaborazione.<br><span style="color:#66706e">'+html.escape(item['path'])+'</span></p>'
                for item in rejected:details+='<p><b>'+html.escape(item['name'])+'</b><br>'+html.escape(item['reason'])+'</p>'
                self.results.setHtml(details)
        self.execute(lambda stop,notify:self.archive.add(paths,stop,notify),done)
    def dragEnterEvent(self,event):
        if event.mimeData().hasUrls() and not self.task:event.acceptProposedAction()
    def dropEvent(self,event):
        self.add([u.toLocalFile() for u in event.mimeData().urls() if u.isLocalFile()]);event.acceptProposedAction()
    def remove_queued(self):
        self.invalidate_timeline()
        ids=[self.docs[i.row()]['id'] for i in self.table.selectionModel().selectedRows() if self.docs[i.row()]['status']=='queued']
        with self.archive.connect() as db:db.executemany("DELETE FROM documents WHERE id=? AND status='queued' AND stored IS NULL",[(id,) for id in ids])
        self.refresh_table();self.status.setText('Rimossi solo gli elementi selezionati non ancora elaborati. Gli originali restano al loro posto.')
    def reset_embeddings(self):
        if self.task:return
        dialog=QMessageBox(QMessageBox.Icon.Warning,'Elimina tutti gli embedding',
            'Vuoi eliminare tutti gli embedding di tutta la biblioteca, anche quelli dei documenti elaborati solo in parte?',parent=self)
        dialog.setInformativeText('Saranno eliminati gli embedding di testi e immagini e i dati della mappa. '
            'I documenti originali e le copie locali resteranno conservati e torneranno in coda. '
            'Per cercare di nuovo dovrai premere “Calcola embedding”.')
        confirm=dialog.addButton('Elimina tutti',QMessageBox.ButtonRole.DestructiveRole)
        cancel=dialog.addButton('Annulla',QMessageBox.ButtonRole.RejectRole)
        dialog.setDefaultButton(cancel);dialog.setEscapeButton(cancel)
        dialog.exec()
        if dialog.clickedButton()!=confirm:return
        self.invalidate_timeline()
        self.need_map=False
        engine=self.engine
        def work(stop,notify):
            if engine:engine.close()
            return self.archive.reset_embeddings(stop,notify)
        def done(result):
            self.engine=None;self.stats={};self.started=None
            self.query.clear();self.search_rows=[];self.map.highlight([]);self.map_done([])
            self.map_search_results.clear()
            self.progress.setValue(0);self.stats_toggle.setChecked(False)
            for key,value in self.metrics.items():value.setText('00:00:00' if key=='elapsed' else '—')
            for key,value in self.perf.items():value.setText('0' if key in {'pages','vectors'} else '—')
            self.backendlabel.setText('Modello scaricato dalla memoria · pronto per un nuovo calcolo')
            message=f"Eliminati {result['embeddings']} embedding. {result['documents']} documenti conservati e rimessi in coda. Premi Calcola embedding per ricalcolarli."
            if result['warning']:message+=' '+result['warning']
            self.results.setPlainText(message);self.status.setText(message)
        self.execute(work,done)
    def open_row(self,i,j):
        doc=self.docs[i];path=self.archive.data/doc['stored'] if doc['stored'] else Path(doc['source']);open_document(path)
    def help(self):
        temporal=('Scoperta indipendente per anno e armonizzazione simmetrica mediante embedding e lessico. Nessun anno iniziale fissa le categorie. Negli anni con pochi passaggi si usa un raggruppamento prudente nei vettori originali. I temi annuali e le corrispondenze sono conservati in temi/temi-annuali.json. '
            if getattr(self,'annual_topics',False) else 'Modello globale appreso da tutti gli anni disponibili. ')
        if self.stable_camera and not self.is_camera:
            QMessageBox.information(self,'Metodo dei saggi', 'Temi scoperti dai testi inglesi con embedding, UMAP, HDBSCAN e c-TF-IDF, senza categorie iniziali. Ricerca aggiuntiva nei residui; i passaggi incerti restano espliciti. '+temporal+'Etichette estrattive da verificare sui brani: non è una classificazione validata da annotatori.\n\nPassaggi entro 1.024 token senza sovrapposizioni. Titoli, date, collegamenti alle traduzioni e ringraziamenti riconosciuti sono separati dai contenuti; note e codice sono conservati. Originali intatti e audit in dati/temi/audit. Date di pubblicazione, mai date di download. I saggi senza data restano ricercabili e sono esclusi dai grafici.\n\nIn vista Passaggi le serie misurano quote di parole, includendo gli incerti nel denominatore. La ricerca misura parole nei passaggi affini: non la percentuale esatta dedicata a un unico argomento. Scala automatica per ricerche e temi selezionati. Proiezione UMAP salvata; vicinanze locali, distanze fra gruppi non quantitative. CSV annuali e mensili in dati/temi.');return
        if self.stable_camera:
            QMessageBox.information(self,'Metodo Camera', 'Temi appresi automaticamente dai testi, senza elenco di categorie predefinite. BERTopic usa gli embedding esistenti; il numero di gruppi emerge dal corpus. '+temporal+'Sono escluse le formule procedurali riconosciute, non gli oratori per ruolo. Audit delle esclusioni in temi/audit.\n\nScoperta automatica esplorativa, ricerca di gruppi nei residui e passaggi incerti espliciti; non è ancora validata su annotazioni umane. Il grafico in vista Passaggi misura quote di parole orali senza sovrapposizioni, escludendo le formule e i tabulati riconosciuti. I temi sono versionati: un nuovo calcolo rigenera tutta la serie temporale. Gli scritti sono esportati separatamente.\n\nCSV mensili/annuali, copertura, campione da revisionare e raggruppamenti dei temi incerti sono in data/camera_deputati/temi. Copertura attuale: sola Assemblea della Camera, non Senato o commissioni.');return
        QMessageBox.information(self,'Limiti e metodo',
            f'Raccolta {self.collection}: testi in {self.corpus}. Il pulsante dedicato aggiunge tutti i testi alla coda; “Aggiungi documenti” permette di sceglierne alcuni. Duplicati identici, anche rinominati, sono segnalati prima del calcolo.\n\n'
            + ('Camera: la ricerca usa testi tematici con pulizia conservativa; PDF, HTML e testi integrali sono conservati. Il registro delle esclusioni è in temi/audit. “Andamento del tema nel tempo” confronta la stessa domanda con tutti i passaggi testuali salvati delle sedute datate, anche parziali. Mostra conteggi, quota sui passaggi indicizzati e sedute complete e parziali per mese/anno. La copertura conta le sole sedute complete. La soglia regolabile è esplorativa; controlla gli estratti. Non misura minuti di discussione né percentuali esatte di parole dedicate al tema.\n\n' if self.is_camera else '') +
            'Testi: TXT/MD e testo DOCX restano interi se entrano in 1.024 token, titolo e contesto ripetuto inclusi. Oltre il limite si preferiscono confini di paragrafo, frase e parola; sovrapposizione fino a 96 token. Nessun testo scartato. TXT/MD: salvataggio dopo ogni passaggio e ripresa dal primo mancante. PDF: estrazione e checkpoint per pagina, con lo stesso limite sui passaggi testuali. Un concetto molto esteso può risultare meno riconoscibile: il modello legge i passaggi, non tutto il saggio insieme. Note ed esempi di codice sono conservati; nessun filtro bibliografia/appendice.\n\n'
            'Velocità: batch di 1, 2, 4 o 8 passaggi per worker; fino a due processi indipendenti. “Confronta” misura e applica la configurazione migliore sul dispositivo selezionato. Più worker non garantiscono maggiore velocità. La GPU viene verificata con inferenza reale; non si passa automaticamente alla CPU. Per i saggi l’encoder visivo non viene caricato.\n\n'
            'Mappa: la scheda conserva la ricerca, i risultati e il grafico annuale. Senza ricerca o gruppo selezionato, le barre impilate contano documenti o passaggi salvati per anno, ripartiti con gli stessi colori e temi della mappa. Una ricerca mostra automaticamente l’andamento del tema; un segmento del grafico seleziona il gruppo. Azzera ricerca torna a tutti i temi. Per Paul Graham si usano le date di pubblicazione dei metadati; i testi senza data restano ricercabili ma sono esclusi dal grafico. Il grafico misura la presenza nei testi disponibili, non la popolarità esterna. Scegli Documenti per il quadro generale o Passaggi di testo per i temi interni ai saggi lunghi. Le etichette dei gruppi sono solo nella legenda in basso. Seleziona un gruppo per evidenziarne tutti i pallini e leggere a destra l’elenco completo dei documenti; clicca un titolo per aprirlo. In vista Passaggi ogni documento compare una sola volta, con le parti/pagine appartenenti al gruppo. Mostra tutti ripristina la mappa completa. La vista 2D è approssimativa. Per la Camera, gruppi e posizioni usano contenuti ripuliti dalle formule procedurali, embedding salvati e argomenti verificati dei resoconti; la vista iniziale è Passaggi di testo. I brani brevi o procedurali sono grigi, presenti anche nei totali. La proiezione t-SNE privilegia vicinanze locali: distanze fra nuvole e loro dimensioni non misurano differenza o importanza dei temi. Per le altre raccolte si usano gli embedding originali e la PCA. La suddivisione è gerarchica: da 5 a 6 gruppi se ne divide uno, senza rimescolare gli altri. Riducendo il numero si riuniscono gli ultimi sottogruppi. Nomi e colori dei gruppi invariati restano gli stessi finché non cambi biblioteca o vista. Auto sceglie un livello dello stesso albero e può lasciare un solo gruppo se la separazione è debole. Link alle traduzioni e ringraziamenti non contribuiscono alle etichette. La ricerca continua a usare tutti i passaggi originali.\n\n'
            'Prestazioni: i tassi escludono il caricamento iniziale. Per TXT una parte è un passaggio di massimo 1.024 token; per PDF si contano le pagine. Il tempo residuo è una stima. Rapporti JSON con tempi, token, memoria e dati per documento.\n\n'
            'Restano supportati PDF, TXT/MD UTF-8, DOCX e immagini. I PDF usano il layout Paperdesk in gruppi di 8 pagine e includono testo, immagini delle pagine e ritagli. Nessun limite al numero totale di pagine imposto dall’app. Scansioni senza OCR testuale. Una singola richiesta bloccata scade dopo 180 secondi.')
    def closeEvent(self,event):
        if self.task:
            self.closing=True;self.cancel();event.ignore();return
        if self.engine:self.engine.close()
        event.accept()

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--data',type=Path,default=APP/'dati');parser.add_argument('--screenshot',type=Path)
    parser.add_argument('--corpus',type=Path,default=DEFAULT_CORPUS);parser.add_argument('--collection',default='My documents');args=parser.parse_args()
    app=QApplication(sys.argv[:1]);app.setApplicationName('GemmaDesk')
    args.data.mkdir(parents=True,exist_ok=True);lock=QLockFile(str(args.data/'app.lock'));lock.setStaleLockTime(0)
    if not lock.tryLock(0):QMessageBox.warning(None,'GemmaDesk','Questo archivio è già aperto o in elaborazione. Se è attiva la rigenerazione Camera, sospendila con: systemctl --user stop gemma-camera-v2');return 1
    QFontDatabase.addApplicationFont(str(APP/'assets/C64Keyboard-Regular.ttf'));app.setStyleSheet(STYLE)
    try:window=Window(Archive(args.data),corpus=args.corpus,collection=args.collection)
    except Exception as e:QMessageBox.critical(None,'GemmaDesk',str(e));return 1
    window.show()
    if args.screenshot:
        def snap():window.grab().save(str(args.screenshot));window.close()
        QTimer.singleShot(2000,snap)
    return app.exec()

if __name__=='__main__':sys.exit(main())
