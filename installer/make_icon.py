"""Generates installer/app.ico (a barcode on a blue rounded square). Run by build.ps1."""
import os

from PIL import Image, ImageDraw

SIZE = 512
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.ico")


def build():
    image = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    # Vertical blue gradient inside a rounded square
    gradient = Image.new("RGBA", (SIZE, SIZE))
    for y in range(SIZE):
        t = y / (SIZE - 1)
        colour = (int(37 + (79 - 37) * t), int(99 + (70 - 99) * t), int(235 + (229 - 235) * t), 255)
        ImageDraw.Draw(gradient).line([(0, y), (SIZE, y)], fill=colour)
    mask = Image.new("L", (SIZE, SIZE), 0)
    ImageDraw.Draw(mask).rounded_rectangle([16, 16, SIZE - 16, SIZE - 16], radius=96, fill=255)
    image.paste(gradient, (0, 0), mask)

    # Barcode bars (width, gap) pairs
    x, top, bottom = 118, 150, 362
    for width, gap in ((22, 16), (10, 14), (30, 16), (10, 12), (22, 18), (10, 14), (34, 0)):
        draw.rectangle([x, top, x + width, bottom], fill=(255, 255, 255, 255))
        x += width + gap
    return image


if __name__ == "__main__":
    build().save(OUT, sizes=[(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (24, 24), (16, 16)])
    print(f"wrote {OUT}")
