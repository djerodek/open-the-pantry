import base64
import mimetypes
import os
from jinja2 import Environment, FileSystemLoader
from weasyprint import HTML

TEMPLATE_DIR = os.path.join(os.path.dirname(__file__), "templates")
_env = Environment(loader=FileSystemLoader(TEMPLATE_DIR), autoescape=True)


def _image_to_data_uri(image_path: str) -> str | None:
    if not image_path or not os.path.exists(image_path):
        return None
    mime, _ = mimetypes.guess_type(image_path)
    mime = mime or "image/jpeg"
    with open(image_path, "rb") as f:
        encoded = base64.b64encode(f.read()).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def render_recipe_html(recipe, absolute_image_path: str | None = None, include_notes: bool = False,
                        include_image: bool = True) -> str:
    """Render the shared export template. Used for both the standalone .html
    download (self-contained, image inlined as base64) and as the input to the
    PDF conversion step -- one template, two outputs.

    include_notes controls whether the recipe's notes (if any) are appended
    as a separate section -- notes are personal annotations, not part of
    the recipe itself, so they're opt-in per export rather than always
    included.

    include_image controls whether the showcase image is embedded --
    defaults to True (HTML export keeps the image), but the PDF export
    endpoint always passes False: the showcase image isn't meant to be
    part of the shared PDF."""
    template = _env.get_template("recipe_export.html")
    image_data_uri = _image_to_data_uri(absolute_image_path) if (absolute_image_path and include_image) else None
    show_notes = include_notes and bool(recipe.notes)
    return template.render(recipe=recipe, image_data_uri=image_data_uri, show_notes=show_notes)


def render_recipe_pdf(recipe, absolute_image_path: str | None = None, include_notes: bool = False,
                       include_image: bool = True) -> bytes:
    html_str = render_recipe_html(recipe, absolute_image_path, include_notes=include_notes, include_image=include_image)
    return HTML(string=html_str).write_pdf()
