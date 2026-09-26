"""Small embedded PNGs for MCP clients that render server and tool icons."""

from mcp.types import Icon

_PNG = {
    "observe": (
        "iVBORw0KGgoAAAANSUhEUgAAACAAAAAgCAYAAABzenr0AAAAzUlEQVR42u1W0RHCMAgNnCPUEXQx"
        "HasuVkewO+iXdxwXXiCNbe7M+w3hPSABUhoY+HdQ9MLl9nqj8+fjTM0FlEi3iKFfEEeEcJR8mae0"
        "zFOyzqKBkPeCdH69r2a0X7ucTS4TVCLXUUnHligkVovgCDk685ZFc7CXoJR6ZIcC4ZwydKEW0qfk4"
        "lqHOkpvdjROW6KqJXW9gb3QjwD5P1ukFpVLcnGkvt7fgZoXLIHuUi0yUeqE/c0CzzREJbLEWSP58"
        "H2g741oj51wYOBwfAC4K3wbxW53PAAAAABJRU5ErkJggg=="
    ),
    "set_motion": (
        "iVBORw0KGgoAAAANSUhEUgAAACAAAAAgCAYAAABzenr0AAAAu0lEQVR42u1XwQ2EMAyjFjNwOx0r"
        "wHSwArfTsQR8EapokjoQ6a5f6thOaeU0za+vpAV003u7+r6On+QioERsFZLYxFoh8CSX1EAt+XdY"
        "qkSAQV4jAiznVhF4+h0A89wtXVB3oEQiEZEVIHEvLV7ad+QCy7l1PzzINbhYt4DpXoqHJ7mkTnsF"
        "fM29SVQOF/8f0CYZVkaIdQvu6MKZI9474NmFXG0wonVNOE3eobRkCMwhw1Ij/mDiPZr91w47PG3N"
        "jlg8lQAAAABJRU5ErkJggg=="
    ),
    "stop_now": (
        "iVBORw0KGgoAAAANSUhEUgAAACAAAAAgCAYAAABzenr0AAAAs0lEQVR42u2XSw7CMAwF01FvUtTe"
        "/zit4CywYlP148TPLgi8jmesxIrsUn49OuvB+zg+a+G3ZTnlEyW35hElt+YTKbdwiJaf8ciQH3HJk"
        "u/xyZRveciWr32Ui+PyAnrLoWGem+CPafLfQKvcmkuU3Mr47Ca0vKG3D1A0kie3V4G+9h/4F0DNA"
        "KmMt4/aKVYp33yC6CLWfFrneYX8sAnVRezx8G423g0JxXrlyUe142X2kjRenylUaZKvtxQAAAAASU"
        "VORK5CYII="
    ),
}


def icon(name: str) -> list[Icon]:
    """Return one self-contained, universally supported 32 px PNG."""
    return [
        Icon(
            src=f"data:image/png;base64,{_PNG[name]}",
            mimeType="image/png",
            sizes=["32x32"],
        )
    ]
