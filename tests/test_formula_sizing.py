import io,os,pathlib,re,sys,tempfile,unittest,zipfile
ROOT=pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import latexexport as L
from epublib import EpubBook
from PIL import Image,ImageDraw

def png(w,h,formula=False):
 im=Image.new('RGB',(w,h),'white')
 if formula:
  d=ImageDraw.Draw(im)
  # Repeated H-shaped connected components at a known pixel font size.
  for y in ([3,38] if h>45 else [3]):
   for x in range(3,w-15,18):
    d.rectangle((x,y,x+2,y+19),fill='black')
    d.rectangle((x+10,y,x+12,y+19),fill='black')
    d.rectangle((x,y+8,x+12,y+10),fill='black')
 else:ImageDraw.Draw(im).rectangle((1,1,w-2,h-2),fill='blue')
 b=io.BytesIO();im.save(b,'PNG');return b.getvalue()

def make_book(path,body,css='',pictures=None):
 pictures=pictures or {'short.png':png(40,20),'tall.png':png(40,60)}
 manifest='<item id="ch" href="ch.xhtml" media-type="application/xhtml+xml"/>'
 for i,n in enumerate(pictures):manifest+=f'<item id="im{i}" href="{n}" media-type="image/png"/>'
 with zipfile.ZipFile(path,'w') as z:
  z.writestr('mimetype','application/epub+zip')
  z.writestr('META-INF/container.xml','<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="book.opf" media-type="application/oebps-package+xml"/></rootfiles></container>')
  z.writestr('book.opf',f'<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="id"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="id">test</dc:identifier><dc:title>Formulas</dc:title><dc:language>en</dc:language></metadata><manifest>{manifest}</manifest><spine><itemref idref="ch"/></spine></package>')
  z.writestr('ch.xhtml',f'<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Formulas</title><style>{css}</style></head><body>{body}</body></html>')
  for n,b in pictures.items():z.writestr(n,b)
 return path

class FormulaSizing(unittest.TestCase):
 def setUp(self):self.tmp=tempfile.TemporaryDirectory();self.folder=pathlib.Path(self.tmp.name)
 def tearDown(self):self.tmp.cleanup()
 def export(self,body,css='',pictures=None,size=11):
  p=make_book(self.folder/'book.epub',body,css,pictures)
  with EpubBook.open(p) as book:
   result=L.write_latex(book,str(self.folder/'out'),'book',L.ExportOptions(font_size=size,cover=False,contents=False,compile_pdf=False))
  return pathlib.Path(result.tex_path).read_text(encoding='utf8'), result
 def test_natural_height_is_not_normalized(self):
  tex,_=self.export('<p>Short <img src="short.png"/> tall <img src="tall.png"/>.</p>')
  widths=re.findall(r'includegraphics\[width=([^,]+)',tex)
  self.assertEqual(widths,['2.50000em','2.50000em'])
 def test_css_height_and_units_override_html_attributes(self):
  tex,_=self.export('<p><img src="short.png" class="a" width="90"/><img src="tall.png" class="b"/></p>', '.a {width:2em;height:3ex} .b{height:2.5em;width:auto}')
  self.assertIn('width=2.00000em,height=3.00000ex',tex)
  self.assertIn('includegraphics[height=2.50000em',tex)
  self.assertNotIn('width=5.62500em',tex)
 def test_source_font_size_converts_css_pixels_to_em(self):
  tex,_=self.export('<p>Test <img src="short.png" width="40"/>.</p>', 'p {font-size:20px}')
  self.assertIn('width=2.00000em',tex)
 def test_css_image_font_size_and_nested_context(self):
  tex,_=self.export('<p style="font-size:2em">Test <img src="short.png" style="font-size:50%;width:2em"/>.</p>')
  self.assertIn('width=1.00000em',tex)
 def test_dimension_coefficients_stay_equal_at_other_export_sizes(self):
  body='<p>Test <img src="short.png" style="height:2em"/>.</p>'
  ten,_=self.export(body,size=10); twelve,_=self.export(body,size=12)
  self.assertEqual(re.findall(r'includegraphics\[([^]]+)',ten),re.findall(r'includegraphics\[([^]]+)',twelve))
 def test_series_calibration_keeps_fraction_height_and_display_scale(self):
  pictures={f'math-{i}.png':png(140,35,True) for i in range(10)}
  pictures['math-10.png']=png(140,75,True)
  body=''.join(f'<p>Formula <img src="math-{i}.png"/> text.</p>' for i in range(10))
  body+='<p>Fraction <img src="math-10.png"/> text.</p><p><img src="math-10.png"/></p>'
  tex,result=self.export(body,pictures=pictures)
  widths=re.findall(r'includegraphics\[width=([^,]+)',tex)
  self.assertEqual(len(set(widths)),1)
  self.assertEqual(len(widths),12)
  self.assertTrue(any('scale estimated' in w for w in result.warnings))
  self.assertIn('raisebox',tex)
 def test_few_icons_are_not_formula_calibrated(self):
  tex,result=self.export('<p>A <img src="short.png"/> and <img src="tall.png"/>.</p>')
  self.assertFalse(result.warnings)
 def test_embedded_svg_uses_source_relative_dimensions(self):
  from PySide6.QtWidgets import QApplication
  app=QApplication.instance() or QApplication([])
  tex,_=self.export('<p>SVG <svg xmlns="http://www.w3.org/2000/svg" width="4em" height="3ex" viewBox="0 0 40 30"><rect width="40" height="30" fill="black"/></svg> text.</p>')
  self.assertIn('width=4.00000em,height=3.00000ex',tex)
 def test_source_max_height_and_width_keep_page_limits(self):
  tex,_=self.export('<p><img src="tall.png" style="max-height:1em"/><img src="short.png" style="width:800px;max-width:1000px"/></p>')
  self.assertIn('max height=1.00000em',tex)
  self.assertIn('width=50.00000em,max width=62.50000em',tex)
  self.assertEqual(tex.count(r'\adjustbox{max width=\linewidth,max height=0.8\textheight}'),2)
 def test_coloured_picture_in_formula_series_is_not_calibrated(self):
  pictures={f'math-{i}.png':png(140,35,True) for i in range(10)}
  pictures['math-999.png']=png(40,60)
  body=''.join(f'<p>Formula <img src="math-{i}.png"/> text.</p>' for i in range(10))
  body+='<p>Illustration <img src="math-999.png"/>.</p>'
  tex,result=self.export(body,pictures=pictures)
  self.assertTrue(any('scale estimated' in w for w in result.warnings))
  self.assertEqual(re.findall(r'includegraphics\[width=([^,]+)',tex)[-1],'2.50000em')
 def test_explicit_formula_series_can_be_all_display_equations(self):
  pictures={f'math-{i}.png':png(140,35,True) for i in range(10)}
  body=''.join(f'<p><img src="math-{i}.png"/></p>' for i in range(10))
  _tex,result=self.export(body,pictures=pictures)
  self.assertTrue(any('scale estimated' in w for w in result.warnings))
 def test_operator_scale_does_not_depend_on_sampling_order(self):
  operator=Image.new('RGB',(40,20),'white');d=ImageDraw.Draw(operator)
  d.rectangle((5,5,34,6),fill='black');d.rectangle((5,12,34,13),fill='black')
  buf=io.BytesIO();operator.save(buf,'PNG')
  widths=[]
  for count in (10,96):
   pictures={f'math-{i}.png':png(140,35,True) for i in range(count)}
   pictures['math-999.png']=buf.getvalue()
   body=''.join(f'<p>Formula <img src="math-{i}.png"/> text.</p>' for i in range(count))
   body+='<p>Operator <img src="math-999.png"/>.</p>'
   tex,_=self.export(body,pictures=pictures)
   widths.append(re.findall(r'includegraphics\[width=([^,]+)',tex)[-1])
  self.assertEqual(widths[0],widths[1])
  self.assertNotEqual(widths[0],'2.50000em')
 def test_numbered_math_table_reserves_width_for_the_formula(self):
  body='<table class="math_table"><tr><td><img src="tall.png"/></td><td>(3.12)</td></tr></table>'
  tex,_=self.export(body,size=12)
  widths=[float(n) for n in re.search(r'\\begin\{longtable\}\{@\{\}(.*?)@\{\}\}',tex)[1].replace('p{','').replace('pt}',' ').split()]
  self.assertGreater(widths[0],250)
  self.assertLess(widths[1],60)
 def test_ordinary_two_column_table_keeps_existing_layout(self):
  body='<table><tr><td><img src="tall.png"/></td><td>(3.12)</td></tr></table>'
  tex,_=self.export(body,size=12)
  widths=[float(n) for n in re.search(r'\\begin\{longtable\}\{@\{\}(.*?)@\{\}\}',tex)[1].replace('p{','').replace('pt}',' ').split()]
  self.assertLess(widths[0],widths[1])

if __name__=='__main__':unittest.main()
