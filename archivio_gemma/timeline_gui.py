"""Interactive timeline with explicit denominator, coverage and source passages."""
import csv
import html
from pathlib import Path
from PySide6.QtCore import Qt, Signal, QRectF
from PySide6.QtGui import QColor, QPainter, QPen, QBrush, QDesktopServices,QPainterPath
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel, QComboBox,
    QDoubleSpinBox, QPushButton, QTableWidget, QTableWidgetItem, QHeaderView,
    QAbstractItemView, QTextBrowser, QWidget, QToolTip, QFileDialog)
from timeline import aggregate, matching_passages
from chart_scale import axis_maximum,tick_label

def document_label(data):return 'Sedute' if data.get('kind','camera')=='camera' else 'Documenti'


def export_csv(path, data, threshold, grouping):
    with open(path, 'w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.writer(stream, delimiter=';')
        label=document_label(data).lower()
        writer.writerow(['tema','soglia_coseno','rilevazione','periodo',label+'_archivio',
            label+'_con_testo_tematico' if label=='sedute' else label+'_con_testo',label+'_indicizzate' if label=='sedute' else label+'_indicizzati',label+'_affini','passaggi_indicizzati',
            'passaggi_affini','quota_passaggi_percento','copertura_'+label+'_percento',label+'_complete' if label=='sedute' else label+'_completi',label+'_parziali',
            'documenti_senza_data','documenti_solo_anno','parole_indicizzate','parole_passaggi_affini','quota_parole_percento'])
        for row in aggregate(data, threshold, grouping):
            percent = lambda k: '' if row[k] is None else round(row[k]*100, 6)
            writer.writerow([data['query'],threshold,data['captured_at'],row['period'],row['all_sessions'],
                row['expected_sessions'],row['indexed_sessions'],row['matched_sessions'],
                row['indexed_passages'],row['matched_passages'],percent('passage_share'),percent('coverage'),
                row['complete_sessions'],row['partial_sessions'],data.get('undated_documents',0),data.get('annual_only_documents',0),row['indexed_words'],row['matched_words'],percent('word_share')])


class TimelineChart(QWidget):
    chosen = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.rows=[];self.metric_key='passage_share';self.regions=[];self.selected=-1;self.document_label='Sedute'
        self.setMinimumHeight(220);self.setMouseTracking(True)
        self.setAccessibleName('Andamento temporale dei passaggi affini al tema')

    def set_rows(self, rows, metric):
        self.rows=rows;self.metric_key=metric;self.selected=-1;self.update()

    def value(self, row):
        if not row['indexed_passages']:
            return None
        value=row[self.metric_key]
        if value is None:return None
        return value*100 if self.metric_key.endswith('_share') else value

    def axis_maximum(self):
        return axis_maximum((self.value(r) for r in self.rows),percent=self.metric_key.endswith('_share'))

    def paintEvent(self, event):
        p=QPainter(self);p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(),QColor('#f6f3e9'));self.regions=[]
        if not self.rows:
            p.drawText(self.rect(),Qt.AlignmentFlag.AlignCenter,'Nessun periodo disponibile.');return
        area=QRectF(55,20,self.width()-75,self.height()-55)
        values=[self.value(r) for r in self.rows]
        maximum=self.axis_maximum()
        for i in range(5):
            y=area.bottom()-area.height()*i/4
            p.setPen(QPen(QColor('#dad8ce'),1));p.drawLine(area.left(),y,area.right(),y)
            p.setPen(QColor('#66706e'));p.drawText(QRectF(0,y-9,48,18),Qt.AlignmentFlag.AlignRight,tick_label(maximum*i/4,maximum))
        step=area.width()/len(self.rows)
        line=QPainterPath();connected=False;dots=[]
        for i,(row,value) in enumerate(zip(self.rows,values)):
            x=area.left()+step*i
            region=QRectF(x,area.top(),step,area.height());self.regions.append(region)
            if value is None:
                p.fillRect(region,QBrush(QColor('#c9c7bc'),Qt.BrushStyle.BDiagPattern))
                connected=False
            else:
                height=area.height()*value/maximum
                color='#b18059' if row['coverage'] is not None and row['coverage']<1 else '#538992'
                px=x+step/2;py=area.bottom()-height
                if connected:line.lineTo(px,py)
                else:line.moveTo(px,py)
                connected=True;dots.append((px,py,color))
            if i==self.selected:
                p.setPen(QPen(QColor('#25333c'),2));p.drawRect(region)
            stride=max(1,len(self.rows)//10)
            if i%stride==0 or i==len(self.rows)-1:
                p.setPen(QColor('#66706e'))
                label_x=max(0,min(self.width()-72,x-22))
                p.drawText(QRectF(label_x,area.bottom()+8,65,22),Qt.AlignmentFlag.AlignCenter,row['period'])
        p.setPen(QPen(QColor('#538992'),2));p.drawPath(line)
        for x,y,color in dots:
            p.setPen(QPen(QColor('#f6f3e9'),1));p.setBrush(QColor(color));p.drawEllipse(QRectF(x-3,y-3,6,6))
        if not any(v is not None for v in values):
            box=QRectF(area.left()+15,area.center().y()-25,area.width()-30,50)
            p.fillRect(box,QColor('#f6f3e9'));p.setPen(QColor('#25333c'))
            p.drawText(box,Qt.AlignmentFlag.AlignCenter,'Nessun passaggio ancora indicizzato: andamento non disponibile.')

    def mouseMoveEvent(self,event):
        for i,rect in enumerate(self.regions):
            if rect.contains(event.position()):
                row=self.rows[i]
                value='Non indicizzato' if not row['indexed_passages'] else f'{row["matched_passages"]} affini su {row["indexed_passages"]} passaggi'
                QToolTip.showText(event.globalPosition().toPoint(),f'{row["period"]}\n{value}\n{self.document_label} con elaborazione completa: {row["complete_sessions"]}/{row["expected_sessions"]}\nCon elaborazione parziale: {row["partial_sessions"]}',self)
                return

    def mousePressEvent(self,event):
        for i,rect in enumerate(self.regions):
            if rect.contains(event.position()):
                self.selected=i;self.update();self.chosen.emit(i);return


class TimelineDialog(QDialog):
    def __init__(self, data, parent=None):
        super().__init__(parent)
        self.data=data;self.matches=[];self.page=0;self.rows=[]
        self.document_label=document_label(data)
        self.setWindowTitle('Tema nel tempo · '+data['query']);self.resize(1100,850)
        layout=QVBoxLayout(self)
        title=QLabel('TEMA: '+data['query']);title.setObjectName('section');title.setWordWrap(True);layout.addWidget(title)
        controls=QHBoxLayout()
        self.grouping=QComboBox();self.grouping.addItem('Per mese','month');self.grouping.addItem('Per anno','year')
        if data.get('kind')=='essays':self.grouping.setCurrentIndex(1)
        controls.addWidget(self.grouping)
        self.metric_combo=QComboBox()
        if data.get('weighted'):self.metric_combo.addItem('Quota parole nei passaggi affini (%)','word_share')
        for name,key in [('Quota di passaggi affini (%)','passage_share'),('Numero di passaggi affini','matched_passages'),(self.document_label+' con passaggi affini','matched_sessions')]:
            self.metric_combo.addItem(name,key)
        controls.addWidget(self.metric_combo);controls.addStretch();controls.addWidget(QLabel('Somiglianza minima'))
        self.threshold=QDoubleSpinBox();self.threshold.setRange(0,1);self.threshold.setSingleStep(.02);self.threshold.setDecimals(2);self.threshold.setValue(.70)
        self.threshold.setToolTip('Soglia esplorativa sul coseno, non una probabilità. Aumentala per restringere i passaggi inclusi; controlla gli esempi sotto il grafico.')
        controls.addWidget(self.threshold)
        save=QPushButton('Esporta CSV');save.clicked.connect(self.save_csv);controls.addWidget(save);layout.addLayout(controls)
        self.summary=QLabel();self.summary.setWordWrap(True);layout.addWidget(self.summary)
        self.chart=TimelineChart();self.chart.document_label=self.document_label;layout.addWidget(self.chart,2)

        self.table=QTableWidget(0,7);self.table.setHorizontalHeaderLabels(['Periodo','Passaggi affini','Passaggi salvati','Quota %',self.document_label+' affini','Completati / previsti',self.document_label+' parziali'])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows);self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers);self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.setMaximumHeight(180);self.table.verticalHeader().hide();layout.addWidget(self.table,1)
        paging=QHBoxLayout();self.detail_label=QLabel('Seleziona un periodo.');paging.addWidget(self.detail_label);paging.addStretch()
        self.previous=QPushButton('Precedenti');self.next=QPushButton('Successivi');paging.addWidget(self.previous);paging.addWidget(self.next);layout.addLayout(paging)
        self.details=QTextBrowser();self.details.setOpenLinks(False);self.details.anchorClicked.connect(self.open_source);layout.addWidget(self.details,2)
        self.previous.clicked.connect(lambda:self.change_page(-1));self.next.clicked.connect(lambda:self.change_page(1))
        self.chart.chosen.connect(self.table.selectRow);self.table.itemSelectionChanged.connect(self.select_period)
        for control in [self.grouping,self.metric_combo]:control.currentIndexChanged.connect(self.refresh)
        self.threshold.valueChanged.connect(self.refresh)
        self.refresh()

    def refresh(self):
        old_period=getattr(self,'period',None)
        self.rows=aggregate(self.data,self.threshold.value(),self.grouping.currentData())
        total=sum(r['expected_sessions'] for r in self.rows);done=sum(r['complete_sessions'] for r in self.rows)
        partials=sum(r['partial_sessions'] for r in self.rows)
        passages=sum(r['indexed_passages'] for r in self.rows);matches=sum(r['matched_passages'] for r in self.rows)
        dates=[s['session_date'] for s in self.data['sessions']]
        interval=f'{min(dates)} — {max(dates)}' if dates else '—'
        activity=f'{matches:,} passaggi affini su {passages:,}' if passages else 'Andamento non ancora disponibile'
        missing=f' · {self.data.get("undated_documents",0)} documenti senza data esclusi.'
        if self.grouping.currentData()=='month' and self.data.get('annual_only_documents'):
            missing+=f' {self.data["annual_only_documents"]} documenti con solo anno visibili nella vista annuale.'
        self.summary.setText(f'{activity} · {self.document_label.lower()}: {done}/{total} con elaborazione completa, {partials} parziali. '
                             f'Periodo: {interval}'+missing)
        self.chart.set_rows(self.rows,self.metric_combo.currentData())
        self.table.blockSignals(True);self.table.clearSelection();self.table.setRowCount(len(self.rows))
        for i,row in enumerate(self.rows):
            known=bool(row['indexed_passages'])
            values=[row['period'],str(row['matched_passages']) if known else '—',str(row['indexed_passages']),
                f'{row["passage_share"]*100:.2f}' if known else '—',str(row['matched_sessions']) if known else '—',
                f'{row["complete_sessions"]}/{row["expected_sessions"]}',str(row['partial_sessions'])]
            for j,value in enumerate(values):self.table.setItem(i,j,QTableWidgetItem(value))
        self.table.blockSignals(False);self.matches=[];self.page=0;self.details.clear()
        if self.grouping.currentData()=='year' and old_period:old_period=old_period[:4]
        selected=next((i for i,r in enumerate(self.rows) if r['period']==old_period),None)
        if selected is not None:self.table.selectRow(selected)
        else:
            if hasattr(self,'period'):del self.period
            self.render_details()

    def select_period(self):
        selected=self.table.selectionModel().selectedRows()
        if not selected:return
        index=selected[0].row();period=self.rows[index]['period'];self.chart.selected=index;self.chart.update()
        self.matches=matching_passages(self.data,period,self.threshold.value());self.page=0;self.period=period;self.render_details()

    def change_page(self,direction):
        self.page=max(0,min((max(1,len(self.matches))-1)//30,self.page+direction));self.render_details()

    def render_details(self):
        start=self.page*30;subset=self.matches[start:start+30]
        self.detail_label.setText(f'{getattr(self,"period","")} · {len(self.matches)} passaggi affini'
            +(f' · {start+1}–{start+len(subset)}' if subset else ''))
        if not hasattr(self,'period'):self.detail_label.setText('Seleziona un periodo per controllare i passaggi.')
        self.previous.setEnabled(self.page>0);self.next.setEnabled(start+30<len(self.matches))
        parts=[]
        for i,r in enumerate(subset,start):
            partial='<br><b>Documento parziale'+(f' · {r["done"]} / {r["total_parts"] or "?"} parti salvate' if r.get('done') is not None else '')+'</b>' if r.get('partial') else ''
            parts.append(f'<p><b>{html.escape(r["title"])}</b> · Parte {r["part"]} · similarità {r["score"]:.3f} '
                f'· <a href="source:{i}">Fonte ufficiale</a>{partial}<br>{html.escape(r["text"])}</p>')
        self.details.setHtml(''.join(parts) or '<p>Nessun passaggio da mostrare. Seleziona un periodo; verifica la copertura e la soglia.</p>')

    def open_source(self,url):
        if url.scheme()!='source':return
        try:row=self.matches[int(url.path())]
        except (ValueError,IndexError):return
        from PySide6.QtCore import QUrl
        source=QUrl(row['url'])
        if source.scheme() in {'http','https'}:QDesktopServices.openUrl(source)

    def save_csv(self):
        path,_=QFileDialog.getSaveFileName(self,'Esporta andamento del tema','andamento-tema.csv','CSV (*.csv)')
        if path:export_csv(path,self.data,self.threshold.value(),self.grouping.currentData())
