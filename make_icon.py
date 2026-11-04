"""Draws the ClaudeLamp icon: a dark rounded tile with a glowing purple light bulb.
Writes docs/images/logo.png (256 px) and an .iconset folder for iconutil. Run: uv run --with pillow make_icon.py <out_dir>"""
import os, sys
from PIL import Image, ImageDraw, ImageFilter

S = 1024
out = sys.argv[1] if len(sys.argv) > 1 else "."
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)
d.rounded_rectangle((60, 60, S - 60, S - 60), radius=230, fill=(11, 11, 16, 255))

# glow behind the bulb
glow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
g = ImageDraw.Draw(glow)
g.ellipse((262, 190, 762, 690), fill=(124, 60, 255, 255))
glow = glow.filter(ImageFilter.GaussianBlur(120))
img.alpha_composite(glow)

# bulb glass: circle plus a neck
d = ImageDraw.Draw(img)
glass = Image.new("RGBA", (S, S), (0, 0, 0, 0))
gd = ImageDraw.Draw(glass)
gd.ellipse((312, 230, 712, 630), fill=(44, 24, 80, 255))
gd.polygon([(392, 560), (632, 560), (600, 690), (424, 690)], fill=(44, 24, 80, 255))
glass = glass.filter(ImageFilter.GaussianBlur(2))
img.alpha_composite(glass)
d = ImageDraw.Draw(img)
d.ellipse((312, 230, 712, 630), outline=(196, 160, 255, 255), width=18)
d.line([(392, 560), (424, 690)], fill=(196, 160, 255, 255), width=18)
d.line([(632, 560), (600, 690)], fill=(196, 160, 255, 255), width=18)
# cap and base
d.rounded_rectangle((424, 690, 600, 740), radius=16, fill=(196, 160, 255, 255))
d.rounded_rectangle((440, 756, 584, 796), radius=14, fill=(150, 116, 220, 255))
d.rounded_rectangle((470, 812, 554, 842), radius=12, fill=(120, 90, 190, 255))
# filament
fil = Image.new("RGBA", (S, S), (0, 0, 0, 0))
fd = ImageDraw.Draw(fil)
fd.line([(470, 600), (470, 470), (512, 410), (554, 470), (554, 600)], fill=(255, 235, 255, 255), width=16, joint="curve")
fd.ellipse((482, 380, 542, 440), fill=(255, 245, 255, 255))
halo = fil.filter(ImageFilter.GaussianBlur(28))
img.alpha_composite(halo)
img.alpha_composite(fil)

mask = Image.new("L", (S, S), 0)
ImageDraw.Draw(mask).rounded_rectangle((60, 60, S - 60, S - 60), radius=230, fill=255)
img.putalpha(mask)

iconset = os.path.join(out, "ClaudeLamp.iconset")
os.makedirs(iconset, exist_ok=True)
for n in (16, 32, 64, 128, 256, 512, 1024):
    im = img.resize((n, n), Image.LANCZOS)
    im.save(os.path.join(iconset, f"icon_{n}x{n}.png"))
    if n >= 32:
        im.save(os.path.join(iconset, f"icon_{n//2}x{n//2}@2x.png"))
os.makedirs("docs/images", exist_ok=True)
img.resize((256, 256), Image.LANCZOS).save("docs/images/logo.png")
print("icon ok")
