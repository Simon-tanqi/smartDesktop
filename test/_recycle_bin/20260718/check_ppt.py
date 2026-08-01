from pptx import Presentation

prs = Presentation(r'D:\PyCharm Project\MyAgent\outputs\presentation.pptx')
print(f'页数: {len(prs.slides)}')
for i, s in enumerate(prs.slides):
    title = s.shapes.title.text if s.shapes.title else "无标题"
    print(f'页{i+1}: {title}')
