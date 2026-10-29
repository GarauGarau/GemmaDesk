"""Batched Qt drawing with exact spatial picking for large embedding maps."""
from collections import defaultdict
import numpy as np
from scipy.spatial import cKDTree
from PySide6.QtCore import QPointF,QRectF,Qt
from PySide6.QtGui import QPolygonF,QColor,QPen,QPainter,QImage
from PySide6.QtWidgets import QGraphicsItem
from map_style import group_color


class PointCloud(QGraphicsItem):
    def __init__(self,items):
        super().__init__();self.items=items;self.state=None;self.filtered=False;self.foreground=np.array([],dtype=int);self.selected=QPolygonF()
        self.xy=np.asarray([(p['x']*550,-p['y']*550) for p in items],dtype=np.float64)
        self.tree=cKDTree(self.xy);self.documents=defaultdict(list);self.groups=defaultdict(list)
        self.labels=np.asarray([p['cluster'] for p in items]);self.doc_ids=np.asarray([p['id'] for p in items])
        self.chunk_ids=np.asarray([p.get('chunk_id') or -1 for p in items])
        for i,p in enumerate(items):self.documents[p['id']].append(i);self.groups[p['cluster']].append(i)
        self.order=np.argsort(self.labels,kind='stable')
        palette={g:np.uint32(0xff000000|int(group_color(g)[1:],16)) for g in self.groups}
        self.colors=np.asarray([palette[int(g)] for g in self.labels],dtype=np.uint32)
        self.partial=self.polygon([i for i,p in enumerate(items) if p.get('partial')])
        low=self.xy.min(axis=0);high=self.xy.max(axis=0)
        self.bounds=QRectF(float(low[0]),float(low[1]),max(.01,float(high[0]-low[0])),max(.01,float(high[1]-low[1]))).adjusted(-12,-12,12,12)

    def polygon(self,indices):
        return QPolygonF([QPointF(float(x),float(y)) for x,y in self.xy[indices]])

    def boundingRect(self):return self.bounds

    def hits(self,x,y,radius):
        indices=self.tree.query_ball_point([x,y],radius)
        return sorted(indices,key=lambda i:float(np.sum((self.xy[i]-[x,y])**2)))

    def style(self,group,matches,match_chunks,selected):
        key=(group,frozenset(matches),None if match_chunks is None else frozenset(match_chunks),selected)
        if key==self.state:return
        self.state=key;self.filtered=group is not None or bool(matches)
        if group is not None:self.foreground=np.asarray(self.groups.get(group,[]),dtype=int)
        elif matches:
            active=np.isin(self.doc_ids,list(matches))
            if match_chunks is not None:active&=(self.chunk_ids==-1)|np.isin(self.chunk_ids,list(match_chunks))
            self.foreground=np.flatnonzero(active)
        else:self.foreground=np.array([],dtype=int)
        self.selected=self.polygon(self.documents.get(selected,[]));self.update()

    def paint(self,painter,option,widget=None):
        # A viewport-sized raster bounds work by visible pixels. NumPy scatters
        # all visible points in C; Qt only composites one image per frame.
        # Picking still queries the complete, unsampled spatial index.
        transform=painter.worldTransform();device=painter.device()
        ratio=device.devicePixelRatioF();width=int(device.width()*ratio);height=int(device.height()*ratio)
        raster=np.zeros((height,width),dtype=np.uint32)
        x=np.rint((self.xy[:,0]*transform.m11()+transform.dx())*ratio).astype(np.int32)
        y=np.rint((self.xy[:,1]*transform.m22()+transform.dy())*ratio).astype(np.int32)
        margin=max(4,int(np.ceil(2*ratio))+1)
        visible=(x>=margin)&(x<width-margin)&(y>=margin)&(y<height-margin)
        def scatter(indices,color,radius):
            indices=indices[visible[indices]]
            if not len(indices):return
            xx=x[indices];yy=y[indices];values=self.colors[indices] if color is None else np.uint32(color)
            for dx in range(-radius,radius+1):
                for dy in range(-radius,radius+1):
                    if dx*dx+dy*dy<=radius*radius+1:raster[yy+dy,xx+dx]=values
        radius=max(1,round((1 if len(self.items)>3000 else 2)*ratio))
        if self.filtered:
            scatter(self.order,0xffd8d8d1,0)
            scatter(self.foreground,None,radius)
        else:scatter(self.order,None,radius)
        image=QImage(raster.data,width,height,raster.strides[0],QImage.Format.Format_ARGB32)
        image.setDevicePixelRatio(ratio)
        painter.save();painter.resetTransform();painter.drawImage(QPointF(0,0),image);painter.restore()
        def draw(polygon,color,width):
            if polygon.isEmpty():return
            pen=QPen(QColor(color),width);pen.setCosmetic(True);pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            painter.setPen(pen);painter.drawPoints(polygon)
        draw(self.partial,'#6f665a',1)
        draw(self.selected,'#25333c',7);draw(self.selected,'#fffdf8',3)
