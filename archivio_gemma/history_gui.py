"""Annual map composition and query history, shown alongside the semantic map."""
import math
from PySide6.QtCore import Qt,Signal,QRectF
from PySide6.QtGui import QColor,QPainter,QPen,QPainterPath
from PySide6.QtWidgets import (QWidget,QVBoxLayout,QHBoxLayout,QLabel,QPushButton,
    QDoubleSpinBox,QComboBox,QStackedWidget,QToolTip)
from timeline import aggregate,yearly_composition
from timeline_gui import TimelineChart,document_label
from map_style import group_label,group_color
from chart_scale import axis_maximum,tick_label


class StackedYearChart(QWidget):
    group_chosen=Signal(int)
    def __init__(self,colors,parent=None):
        super().__init__(parent);self.colors=colors;self.data={'rows':[],'groups':{},'scope':'documents'}
        self.group=None;self.regions=[];self.setMouseTracking(True);self.setMinimumHeight(120)
        self.setAccessibleName('Distribuzione annuale dei temi, barre impilate')

    def set_data(self,data,group=None):
        self.data=data;self.group=group;self.regions=[];self.update()

    def totals(self):
        return [r['total'] if self.group is None else r['groups'].get(self.group,0) for r in self.data['rows']]

    def axis_maximum(self):
        weighted=self.data.get('weighted',False)
        values=self.totals()
        if weighted:values=[100*v/r['total'] if r['total'] else None for v,r in zip(values,self.data['rows'])]
        return axis_maximum(values,percent=weighted,full=weighted and self.group is None)

    def paintEvent(self,event):
        painter=QPainter(self);painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(),QColor('#f6f3e9'));self.regions=[]
        rows=self.data['rows']
        if not rows:
            painter.drawText(self.rect(),Qt.AlignmentFlag.AlignCenter,'Nessun documento con data disponibile.');return
        area=QRectF(46,12,max(1,self.width()-62),max(1,self.height()-42))
        weighted=self.data.get('weighted',False)
        maximum=self.axis_maximum()
        for i in range(5):
            y=area.bottom()-area.height()*i/4
            painter.setPen(QPen(QColor('#dad8ce'),1));painter.drawLine(area.left(),y,area.right(),y)
            painter.setPen(QColor('#66706e'));painter.drawText(QRectF(0,y-9,40,18),Qt.AlignmentFlag.AlignRight,tick_label(maximum*i/4,maximum))
        step=area.width()/len(rows)
        groups=sorted(self.data['groups']) if self.group is None else [self.group]
        stride=max(1,math.ceil(len(rows)/max(1,area.width()/60)))
        line=QPainterPath();connected=False;dots=[]
        for index,row in enumerate(rows):
            x=area.left()+step*index;bottom=area.bottom()
            if self.group is not None:
                count=row['groups'].get(self.group,0)
                if row['total']:
                    value=100*count/row['total'] if weighted else count
                    px=x+step/2;py=area.bottom()-area.height()*value/maximum
                    if connected:line.lineTo(px,py)
                    else:line.moveTo(px,py)
                    connected=True;dots.append((px,py,row,count))
                    self.regions.append((QRectF(px-6,py-6,12,12),row,self.group,count))
                else:connected=False
            for group in groups:
                if self.group is not None:continue
                count=row['groups'].get(group,0)
                if not count:continue
                height=area.height()*(100*count/row['total'] if weighted else count)/maximum
                rect=QRectF(x+step*.12,bottom-height,step*.76,height);bottom-=height
                painter.fillRect(rect,QColor(group_color(group,self.colors)))
                self.regions.append((rect,row,group,count))
            if weighted and row.get('coverage') is not None and row['coverage']<1:
                painter.fillRect(QRectF(x+step*.12,area.bottom()+1,step*.76,3),QColor('#b2823b'))
            if index%stride==0 or index==len(rows)-1:
                painter.setPen(QColor('#66706e'))
                painter.drawText(QRectF(x+step/2-25,area.bottom()+5,50,20),Qt.AlignmentFlag.AlignCenter,row['period'])
        if self.group is not None:
            color=QColor(group_color(self.group,self.colors));painter.setPen(QPen(color,2));painter.drawPath(line)
            for x,y,row,count in dots:
                painter.setPen(QPen(QColor('#f6f3e9'),1));painter.setBrush(color);painter.drawEllipse(QRectF(x-3,y-3,6,6))
        if not any(self.totals()):
            painter.setPen(QColor('#25333c'))
            painter.drawText(area,Qt.AlignmentFlag.AlignCenter,'Nessun elemento datato già indicizzato in questa vista.')

    def mouseMoveEvent(self,event):
        for rect,row,group,count in self.regions:
            if rect.contains(event.position()):
                unit='parole' if self.data.get('weighted') else 'passaggi' if self.data['scope']=='passages' else 'documenti'
                text=f'{row["period"]} · {group_label(group)} · {self.data["groups"][group]}\n{count} {unit} su {row["total"]} nell’anno\nClic: seleziona il tema nella mappa'
                if self.data.get('weighted'):
                    text+=f'\nQuota: {100*count/row["total"]:.1f}% · copertura sedute complete: {row["complete_sessions"]}/{row["expected_sessions"]}'
                QToolTip.showText(event.globalPosition().toPoint(),text,self);return
        QToolTip.hideText()

    def mousePressEvent(self,event):
        for rect,row,group,count in self.regions:
            if rect.contains(event.position()):self.group_chosen.emit(group);return


class HistoryPanel(QWidget):
    group_chosen=Signal(int);reset_requested=Signal();detail_requested=Signal(str)
    def __init__(self,colors,parent=None):
        super().__init__(parent);self.mode='overview';self.query_data=None;self.rows=[]
        self.setMinimumHeight(200)
        layout=QVBoxLayout(self);layout.setContentsMargins(10,5,10,5);layout.setSpacing(4)
        header=QHBoxLayout();self.title=QLabel('TEMI NEL TEMPO · documenti per anno');self.title.setObjectName('section')
        self.title.setWordWrap(True);self.title.setMaximumHeight(44);header.addWidget(self.title,1)
        self.metric_selector=QComboBox();self.metric_selector.addItem('Quota passaggi (%)','passage_share');self.metric_selector.addItem('Passaggi affini','matched_passages');self.metric_selector.addItem('Documenti affini','matched_sessions')
        header.addWidget(self.metric_selector)
        self.threshold_label=QLabel('Soglia');header.addWidget(self.threshold_label)
        self.threshold=QDoubleSpinBox();self.threshold.setRange(0,1);self.threshold.setSingleStep(.02);self.threshold.setDecimals(2);self.threshold.setValue(.70);header.addWidget(self.threshold)
        self.threshold.setToolTip('Somiglianza minima del passaggio alla ricerca; non è una probabilità.')
        self.reset=QPushButton('Tutti i temi');self.reset.clicked.connect(self.reset_requested.emit);header.addWidget(self.reset)
        self.details=QPushButton('Dettagli');self.details.clicked.connect(lambda:self.detail_requested.emit(''));header.addWidget(self.details)
        for button in (self.reset,self.details):button.setStyleSheet('font-size:12px;padding:4px 9px;border-bottom-width:2px;')
        layout.addLayout(header)
        self.charts=QStackedWidget();self.stacked=StackedYearChart(colors);self.query_chart=TimelineChart();self.query_chart.setMinimumHeight(120)
        self.charts.addWidget(self.stacked);self.charts.addWidget(self.query_chart);layout.addWidget(self.charts,1)
        self.stacked.group_chosen.connect(self.group_chosen.emit)
        self.query_chart.chosen.connect(lambda index:self.detail_requested.emit(self.rows[index]['period']))
        self.threshold.valueChanged.connect(self.refresh_query);self.metric_selector.currentIndexChanged.connect(self.refresh_query)
        self.show_overview({'sessions':[]},[],'documents')

    def show_overview(self,catalog,points,scope,group=None):
        self.mode='overview' if group is None else 'group';self.query_data=None
        key=(id(catalog),id(points),scope)
        if getattr(self,'_composition_key',None)!=key:
            self._composition=yearly_composition(catalog,points,scope);self._composition_key=key
            self._composition_sources=(catalog,points)
        data=self._composition;self.stacked.set_data(data,group);self.charts.setCurrentWidget(self.stacked)
        unit='passaggi' if scope=='passages' else 'documenti'
        title=f'TEMI NEL TEMPO · {unit}' if group is None else f'{group_label(group)} · {data["groups"].get(group,"")} · {unit}'
        if data.get('weighted'):title=title.replace('passaggi','% parole')
        self.title.setText(title);self.title.setToolTip(title)
        for widget in (self.metric_selector,self.threshold_label,self.threshold,self.details):widget.hide()
        self.reset.setVisible(group is not None)

    def show_query(self,data):
        same_query=self.query_data is data
        self.mode='query';self.query_data=data;self.charts.setCurrentWidget(self.query_chart)
        for widget in (self.metric_selector,self.threshold_label,self.threshold,self.details,self.reset):widget.show()
        current=self.metric_selector.currentData()
        self.metric_selector.blockSignals(True);self.metric_selector.clear()
        choices=([('Quota parole nei passaggi affini (%)','word_share')] if data.get('weighted') else [])+[('Quota passaggi (%)','passage_share'),('Passaggi affini','matched_passages'),('Documenti affini','matched_sessions')]
        for name,key in choices:self.metric_selector.addItem(name,key)
        if same_query and self.metric_selector.findData(current)>=0:self.metric_selector.setCurrentIndex(self.metric_selector.findData(current))
        self.metric_selector.blockSignals(False)
        self.title.setText('TEMA: '+data['query']);self.title.setToolTip(data['query'])
        self.refresh_query()

    def refresh_query(self):
        if self.mode!='query' or self.query_data is None:return
        self.rows=aggregate(self.query_data,self.threshold.value(),'year')
        self.query_chart.document_label=document_label(self.query_data)
        self.query_chart.set_rows(self.rows,self.metric_selector.currentData())
        self.query_chart.setToolTip('Quota di parole nei passaggi affini; soglia sul coseno. Clicca un anno per gli estratti.')
