from math import ceil
from mathutils import Matrix
import bpy
import numpy as np

from .. import prefs
from ..log import log, logd, logger
from ..rbf import *

class GRET_OT_retarget_mesh(bpy.types.Operator):
    """Retarget meshes to fit a modified version of the source mesh"""

    bl_idname = 'gret.retarget_mesh'
    bl_label = "Retarget Mesh"
    bl_options = {'INTERNAL', 'UNDO'}

    source: bpy.props.StringProperty(
        name="Source",
        description="Source mesh object that the meshes were originally fit to",
    )
    destination: bpy.props.StringProperty(
        name="Destination",
        description="Modified mesh object to retarget to",
    )
    invert: bpy.props.BoolProperty(
        name="Invert",
        description="Swap source and destination meshes, produces the inverse result",
        default=False,
    )
    use_object_transform: bpy.props.BoolProperty(
        name="Use Object Transform",
        description="Evaluate source and destination meshes in global space",
        default=False,
    )
    use_shape_key: bpy.props.BoolProperty(
        name="Use Shape Key",
        description="Destination is the name of a shape key in the source mesh",
        default=False,
    )
    function: bpy.props.EnumProperty(
        items=[
            ('LINEAR', "Linear", "Linear function"),
            ('GAUSSIAN', "Gaussian", "Gaussian function"),
            ('PLATE', "Thin Plate", "Thin plate function"),
            ('BIHARMONIC', "Biharmonic", "Multi quadratic biharmonic"),
            ('INV_BIHARMONIC', "Inverse Biharmonic", "Inverse multi quadratic biharmonic"),
            ('C2', "C2", "Beckert-Wendland C2 basis"),
        ],
        name="Function",
        description="Radial basis function kernel",
        default='BIHARMONIC',  # Least prone to explode and not too slidy
    )
    radius: bpy.props.FloatProperty(
        name="Radius",
        description="Smoothing parameter for the radial basis function",
        subtype='DISTANCE',
        default=0.5,
        min=0.0,
    )
    only_selection: bpy.props.BoolProperty(
        name="Only Vertex Selection",
        description="""Sample only the current vertex selection of the source mesh.
Use to speed up retargeting by selecting only the areas of importance""",
        default=False,
    )
    high_quality: bpy.props.BoolProperty(
        name="High Quality",
        description="Sample more vertices for higher accuracy. Slow on dense meshes",
        default=False,
    )
    use_mirror_x: bpy.props.BoolProperty(
        name="X-Axis Mirror",
        description="""Enable X symmetry of the source mesh.
Doubles the input vertex count, don't enable if not necessary""",
        default=False,
    )
    as_shape_key: bpy.props.BoolProperty(
        name="As Shape Key",
        description="Save the result as a shape key",
        default=False,
    )

    @classmethod
    def poll(cls, context):
        return context.mode == 'OBJECT' and bool(context.selected_objects)

    def execute(self, context):
        src_obj = bpy.data.objects.get(self.source)
        if self.use_shape_key:
            dst_obj, dst_shape_key_name = src_obj, self.destination
        else:
            dst_obj, dst_shape_key_name = bpy.data.objects.get(self.destination), None
        assert src_obj and dst_obj and src_obj.type == 'MESH' and dst_obj.type == 'MESH'

        num_vertices = len(src_obj.data.vertices)
        if num_vertices == 0:
            self.report({'ERROR'}, "Source mesh has no vertices.")
            return {'CANCELLED'}
        if num_vertices != len(dst_obj.data.vertices):
            self.report({'ERROR'}, "Source and destination meshes must have equal number of vertices.")
            return {'CANCELLED'}

        # Sampling many vertices in a dense mesh doesn't change the result that much, vertex stride
        # allows trading accuracy for speed. This assumes vertices close together have sequential
        # indices, which is not always the case.
        if self.high_quality:
            vertex_cap = prefs.retarget__max_vertices_high
        else:
            vertex_cap = prefs.retarget__max_vertices_low
        mask = [v.select for v in src_obj.data.vertices] if self.only_selection else None
        num_masked = sum(mask) if mask else num_vertices
        stride = ceil(num_masked / vertex_cap)
        if num_masked == 0:
            self.report({'ERROR'}, "Source mesh has no vertices selected.")
            return {'CANCELLED'}
        logd(f"num_verts={num_masked}/{num_vertices} stride={stride} total={num_masked//stride}")

        rbf_kernel, scale = rbf_kernels.get(self.function, (linear, 1.0))
        x_mirror = [] if self.use_mirror_x else None
        src_pts = get_mesh_points(src_obj,
            mask=mask, stride=stride, x_mirror=x_mirror)
        if not x_mirror:
            x_mirror = None  # No vertices to mirror, prevent it from attempting to mirror again
        dst_pts = get_mesh_points(dst_obj, shape_key=dst_shape_key_name,
            mask=mask, stride=stride, x_mirror=x_mirror)
        if self.invert:
            src_pts, dst_pts = dst_pts, src_pts
        weights = get_weight_matrix(src_pts, dst_pts, rbf_kernel, self.radius * scale)
        if weights is None:
            self.report({'ERROR'}, "Failed to retarget. Try a different function or radius.")
            return {'CANCELLED'}

        for obj in context.selected_objects:
            if obj.type != 'MESH' or obj == src_obj or obj == dst_obj:
                continue

            if self.use_object_transform:
                # Get the mesh points in retarget destination space
                dst_to_obj = obj.matrix_world.inverted() @ dst_obj.matrix_world
            else:
                dst_to_obj = Matrix()
            obj_to_dst = dst_to_obj.inverted()
            pts = get_mesh_points(obj, matrix=obj_to_dst)
            num_pts = pts.shape[0]
            if num_pts == 0:
                continue

            dist = get_distance_matrix(pts, src_pts, rbf_kernel, self.radius * scale)
            identity = np.ones((num_pts, 1))
            h = np.bmat([[dist, identity, pts]])
            new_pts = np.asarray(np.dot(h, weights))

            shape_key_name = None
            if self.as_shape_key:
                shape_key_name = f"Retarget_{dst_obj.name}"
                if self.use_shape_key:
                    shape_key_name += f"_{dst_shape_key_name}"
            set_mesh_points(obj, new_pts, matrix=dst_to_obj, shape_key_name=shape_key_name)
            obj.data.update()

        return {'FINISHED'}

def draw_panel(self, context):
    layout = self.layout
    settings = context.scene.gret
    obj = context.object

    box = layout.box()
    box.label(text="Retarget Mesh", icon='MOD_MESHDEFORM')
    col = box.column(align=False)

    row = col.row(align=True)
    row.prop(settings, 'retarget_src', text="")
    icon = 'BACK' if settings.retarget_invert else 'FORWARD'
    row.prop(settings, 'retarget_invert', text="", icon=icon, emboss=settings.retarget_invert)
    row.prop(settings, 'retarget_dst', text="")

    row = col.row()
    row.alignment = 'LEFT'
    icon = 'DOWNARROW_HLT' if settings.retarget_show_advanced else 'RIGHTARROW'
    row.prop(settings, 'retarget_show_advanced', icon=icon, emboss=False)

    if settings.retarget_show_advanced:
        row = col.row(align=True)
        row.prop(settings, 'retarget_function', text="")
        row.prop(settings, 'retarget_radius', text="")

        col.prop(settings, 'retarget_only_selection')
        col.prop(settings, 'retarget_high_quality')
        col.prop(settings, 'retarget_use_mirror_x')

    col.separator()

    row = col.row(align=True)
    op1 = row.operator('gret.retarget_mesh', icon='CHECKMARK', text="Retarget")
    op2 = row.operator('gret.retarget_mesh', icon='SHAPEKEY_DATA', text="To Shape Key")
    if settings.retarget_src and settings.retarget_dst != 'NONE':
        op1.source = op2.source = settings.retarget_src.name
        op1.use_shape_key = op2.use_shape_key = settings.retarget_dst.startswith('s_')
        op1.destination = op2.destination = settings.retarget_dst[2:]
        op1.invert = op2.invert = settings.retarget_invert
        op1.function = op2.function = settings.retarget_function
        op1.radius = op2.radius = settings.retarget_radius
        op1.only_selection = op2.only_selection = settings.retarget_only_selection
        op1.high_quality = op2.high_quality = settings.retarget_high_quality
        op1.use_mirror_x = op2.use_mirror_x = settings.retarget_use_mirror_x
        op1.as_shape_key = False
        op2.as_shape_key = True
    else:
        row.enabled = False

def retarget_src_update(self, context):
    # On changing the source object, reset the destination object
    context.scene.gret.retarget_dst = 'NONE'

items = []
def retarget_dst_items(self, context):
    # Return shape keys of the source object and mesh objects with the same amount of vertices
    settings = context.scene.gret
    src_obj = settings.retarget_src

    items.clear()
    items.append(('NONE', "", ""))
    if src_obj:
        src_mesh = src_obj.data
        for o in context.scene.objects:
            if o.type == 'MESH' and o != src_obj and len(o.data.vertices) == len(src_mesh.vertices):
                items.append(('o_' + o.name, o.name, f"Object '{o.name}'", 'OBJECT_DATA', len(items)))
        if src_mesh.shape_keys:
            for sk in src_mesh.shape_keys.key_blocks:
                items.append(('s_' + sk.name, sk.name, f"Shape Key '{sk.name}'", 'SHAPEKEY_DATA', len(items)))
    return items

def register(settings, prefs):
    if not prefs.retarget__enable:
        return False

    bpy.utils.register_class(GRET_OT_retarget_mesh)

    settings.add_property('retarget_src', bpy.props.PointerProperty(
        name="Mesh Retarget Source",
        description="Original mesh that the meshes are fit to",
        type=bpy.types.Object,
        poll=lambda self, obj: obj and obj.type == 'MESH',
        update=retarget_src_update,
    ))
    settings.add_property('retarget_dst', bpy.props.EnumProperty(
        name="Mesh Retarget Destination",
        description="""Mesh or shape key to retarget to.
Expected to share topology and vertex order with the source mesh""",
        items=retarget_dst_items,
    ))
    settings.add_property('retarget_show_advanced', bpy.props.BoolProperty(
        name="Advanced Options",
        description="Show advanced options",
    ))
    retarget_props = GRET_OT_retarget_mesh.__annotations__
    settings.add_property('retarget_invert', retarget_props['invert'])
    settings.add_property('retarget_function', retarget_props['function'])
    settings.add_property('retarget_radius', retarget_props['radius'])
    settings.add_property('retarget_only_selection', retarget_props['only_selection'])
    settings.add_property('retarget_high_quality', retarget_props['high_quality'])
    settings.add_property('retarget_use_mirror_x', retarget_props['use_mirror_x'])

def unregister():
    bpy.utils.unregister_class(GRET_OT_retarget_mesh)
