import zipfile
import xml.etree.ElementTree as ET

path = r'e:\\Hackathon\\Smart_Campus_Energy_Optimization\\BUP_CSE_FEST_2026_Preliminary_Problem_Statement_GridWise_LLM.docx'
z = zipfile.ZipFile(path)
xml = z.read('word/document.xml').decode('utf-8')

W = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
root = ET.fromstring(xml)
body = root.find(W + 'body')

paragraphs = []
for p in body.iter(W + 'p'):
    txt = ''.join(t.text or '' for t in p.iter(W + 't'))
    paragraphs.append(txt)

print('\n'.join(paragraphs))
