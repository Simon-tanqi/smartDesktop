import pathlib
p = pathlib.Path("D:\\PyCharm Project\\MyAgent\\memory\\context_router.py")
t = p.read_text("utf-8")
# replace the busted line (missing $ and stray ')
old = """            r'^(?:可以了|就这样|好了|完事|done|finish|不错|满意|行了|没事了|没其他事|没别的|没什么)','"""
new = """            r'^(?:可以了|就这样|好了|完事|done|finish|不错|满意|行了|没事了|没其他事|没别的|没什么)$',"""
t = t.replace(old, new)
p.write_text(t, "utf-8")
print("done")
