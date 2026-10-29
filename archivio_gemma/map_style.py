"""Shared identities for map dots, topic legend and yearly stacked bars."""
COLORS=['#538992','#b18059','#778d68','#977b97','#b59c52','#6686a5','#ad7070','#8f9583',
        '#686c89','#9a835f','#5d9488','#aa8296','#347bba','#d07936','#669d3e','#ab579a',
        '#a4a036','#4862a3','#c05461','#45805b','#8061b0','#a26835','#358f9b','#bc6386']
RESIDUAL_COLOR='#a5a6a1'

def group_label(group):return f'G{group+1}' if group>=0 else {-1:'Da verificare',-2:'Esclusi',-3:'Da elaborare'}.get(group,'Altri passaggi')

def group_color(group,colors=COLORS):
    if group<0:return RESIDUAL_COLOR
    if group<len(colors):return colors[group]
    import colorsys
    rgb=colorsys.hls_to_rgb((group*.618033988749895)%1,.47,.46)
    return '#'+''.join(f'{round(c*255):02x}' for c in rgb)
