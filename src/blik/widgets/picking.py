import napari
import numpy as np
import pandas as pd
from dask.array import compute
from magicgui import magic_factory, magicgui
from magicgui.widgets import Container
from morphosamplers.helical_filament import HelicalFilament
from morphosamplers.models import Sphere
from morphosamplers.preprocess import get_label_paths_3d
from morphosamplers.sampler import (
    sample_volume_along_spline,
    sample_volume_around_surface,
)
from morphosamplers.samplers.sphere_samplers import PointSampler, PoseSampler
from morphosamplers.surface_spline import GriddedSplineSurface
from scipy.spatial import ConvexHull
from scipy.spatial.transform import Rotation

from ..reader import construct_particle_layer_tuples
from ..utils import invert_xyz


def _generate_surface_grids_from_shapes_layer(
    surface_shapes,
    spacing_A=100,
    inside_points=None,
    closed=False,
):
    """create a new surface representation from picked surface points."""
    spacing_A /= surface_shapes.scale[0]
    colors = []
    surface_grids = []
    data_array = np.array(surface_shapes.data, dtype=object)  # helps with indexing
    if inside_points is None:
        inside_point = None
    else:
        inside_point = (
            invert_xyz(inside_points.data[0]) if len(inside_points.data) else None
        )
    for _, surf in surface_shapes.features.groupby("surface_id"):
        lines = data_array[surf.index]
        # sort by z so lines can be added in between at a later point
        # also invert xyz so we operate in back in xyz world and not napari inverted world
        lines = [
            invert_xyz(line.astype(float))
            for line in sorted(lines, key=lambda x: x[0, 0])
        ]

        # drop duplicate points (messes up scipy's fitpack for splines)
        lines = [pd.DataFrame(line).drop_duplicates().to_numpy() for line in lines]

        try:
            surface_grids.append(
                GriddedSplineSurface(
                    points=lines,
                    separation=spacing_A,
                    order=3,
                    closed=closed,
                    inside_point=inside_point,
                )
            )
        except ValueError:
            continue

        colors.append(surface_shapes.edge_color[surf.index])

    if not colors:
        raise RuntimeError("could not generate surfaces for some reason")

    colors = np.concatenate(colors)
    return surface_grids, colors


def _generate_surface_grids_from_labels_layer(
    surface_label,
    spacing_A=100,
    inside_points=None,
    closed=False,
):
    """create a new surface representation from a segmentation."""
    spacing_A /= surface_label.scale[0]
    surface_grids = []
    if inside_points is None:
        inside_point = None
    else:
        inside_point = (
            invert_xyz(inside_points.data[0]) if len(inside_points.data) else None
        )

    # doing this "custom" because we need to flip xyz
    surfaces_lines = get_label_paths_3d(
        compute(surface_label.data)[0], axis=0, slicing_step=10, sampling_step=10
    )

    for lines in surfaces_lines:
        lines = [invert_xyz(line.astype(float)) for line in lines]

        try:
            surface_grids.append(
                GriddedSplineSurface(
                    points=lines,
                    separation=spacing_A,
                    order=3,
                    closed=closed,
                    inside_point=inside_point,
                )
            )
        except ValueError:
            continue

    return surface_grids, np.random.rand(len(surface_grids), 3)


def _resample_surfaces(image_layer, surface_grids, spacing, thickness, masked):
    volumes = []
    for surf in surface_grids:
        vol = sample_volume_around_surface(
            compute(image_layer.data)[0].T,  # transpose to go back to xyz world
            surface=surf,
            sampling_thickness=thickness,
            sampling_spacing=spacing,
            interpolation_order=3,
            masked=masked,
        )
        volumes.append(vol)
    return volumes


def _generate_filaments_from_points_layer(filament_picks):
    """create a new filament representation from picked points."""
    # invert xyz to go back to xyz world
    points = invert_xyz(filament_picks.data.astype(float))
    # drop duplicate points (messes up scipy's fitpack for splines)
    points = pd.DataFrame(points).drop_duplicates().to_numpy()
    return HelicalFilament(points=points)


def _resample_filament(image_layer, filament, spacing, thickness):
    return sample_volume_along_spline(
        compute(image_layer.data)[0].T,  # transpose to go back to xyz world
        spline=filament,
        sampling_shape=(thickness, thickness),
        sampling_spacing=spacing,
        interpolation_order=3,
    )


@magicgui(
    labels=True,
    call_button="Generate",
    spacing_A={"widget_type": "FloatSlider", "min": 0.01, "max": 1000},
    inside_points={"nullable": True},
)
def surface(
    surface_input: napari.layers.Layer,
    inside_points: napari.layers.Points,
    spacing_A=50,
    closed=False,
) -> napari.types.LayerDataTuple:
    """create a new surface representation from picked surface points."""
    if isinstance(surface_input, napari.layers.Shapes):
        surface_grids, colors = _generate_surface_grids_from_shapes_layer(
            surface_input,
            spacing_A,
            inside_points=inside_points,
            closed=closed,
        )
    else:
        surface_grids, colors = _generate_surface_grids_from_labels_layer(
            surface_input,
            spacing_A,
            inside_points=inside_points,
            closed=closed,
        )

    meshes = []
    exp_id = surface_input.metadata["experiment_id"]

    for surf in surface_grids:
        meshes.append(surf.mesh())

    offset = 0
    vert = []
    faces = []
    ids = []
    for surf_id, (v, f) in enumerate(meshes):
        f += offset
        offset += len(v)
        vert.append(v)
        faces.append(f)
        ids.append(np.full(len(v), surf_id))
    vert = np.concatenate(vert)
    faces = np.concatenate(faces)
    uniq_colors, idx = np.unique(colors, axis=0, return_index=True)
    colormap = uniq_colors[np.argsort(idx)]
    values = np.concatenate(ids) / len(colormap)
    # special case for colormap with 1 color because blacks get autoadded at index 0
    if colormap.shape[0] == 1:
        values += 1

    # invert_xyz to go back to napari world (also invert faces order to preserve normals)
    surface_layer_tuple = (
        (invert_xyz(vert), invert_xyz(faces), values),
        {
            "name": f"{exp_id} - surface",
            "metadata": {
                "experiment_id": exp_id,
                "surface_grids": surface_grids,
                "surface_colors": colors,
            },
            "scale": surface_input.scale,
            "shading": "smooth",
            "colormap": colormap,
        },
        "surface",
    )
    return [surface_layer_tuple]


@magicgui(
    labels=True,
    call_button="Generate",
    spacing_A={"widget_type": "FloatSlider", "min": 0.01, "max": 10000},
)
def surface_particles(
    surface: napari.layers.Surface,
    spacing_A=50,
    masked=False,
) -> napari.types.LayerDataTuple:
    surface_grids = surface.metadata.get("surface_grids", None)
    if surface_grids is None:
        raise ValueError("This surface layer contains no surface grid object.")
    colors = surface.metadata.get("surface_colors")

    exp_id = surface.metadata["experiment_id"]
    spacing = spacing_A / surface.scale[0]

    pos_all = []
    ori_all = []
    for surf in surface_grids:
        if not np.isclose(surf.separation, spacing):
            surf.separation = spacing
        pos = surf.sample()
        ori = surf.sample_orientations()
        if masked:
            pos = pos[surf.mask]
            ori = ori[surf.mask]
        pos_all.append(pos)
        ori_all.append(ori)

    pos_all = np.concatenate(pos_all)
    features = pd.DataFrame({"orientation": np.asarray(Rotation.concatenate(ori_all))})

    return construct_particle_layer_tuples(
        coords=pos_all,
        features=features,
        scale=surface.scale[0],
        exp_id=exp_id,
        face_color_cycle=colors,
        name_suffix="surface picked",
    )


@magicgui(
    labels=True,
    call_button="Resample",
    spacing_A={"widget_type": "FloatSlider", "min": 0.01, "max": 10000},
    thickness_A={"widget_type": "FloatSlider", "min": 0.01, "max": 10000},
)
def resample_surface(
    surface: napari.layers.Surface,
    volume: napari.layers.Image,
    spacing_A=5,
    thickness_A=200,
    masked=False,
) -> napari.types.LayerDataTuple:
    surface_grids = surface.metadata.get("surface_grids", None)
    if surface_grids is None:
        raise ValueError("This surface layer contains no surface grid object.")

    exp_id = surface.metadata["experiment_id"]
    spacing = spacing_A / surface.scale[0]
    thickness = int(np.round(thickness_A / surface.scale[0]))
    for surf in surface_grids:
        if not np.isclose(surf.separation, spacing):
            surf.separation = spacing

    vols = _resample_surfaces(volume, surface_grids, spacing, thickness, masked)

    v = napari.Viewer()
    for i, vol in enumerate(vols):
        v.add_image(
            vol,
            name=f"{exp_id} - surface_{i} resampled",
            metadata={"experiment_id": exp_id, "stack": False},
            scale=surface.scale,
        )


@magicgui(
    labels=True,
    call_button="Generate",
)
def filament(
    points: napari.layers.Points,
) -> napari.types.LayerDataTuple:
    filament = _generate_filaments_from_points_layer(points)

    exp_id = points.metadata["experiment_id"]

    path = invert_xyz(filament.sample(n_samples=len(points.data) * 50))
    shapes_layer_tuple = (
        [path],
        {
            "name": f"{exp_id} - filament",
            "metadata": {
                "experiment_id": exp_id,
                "helical_filament": filament,
            },
            "scale": points.scale,
            "shape_type": "path",
        },
        "shapes",
    )
    return [shapes_layer_tuple]


@magicgui(
    labels=True,
    call_button="Generate",
    rise_A={"widget_type": "FloatSlider", "min": 0.01, "max": 10000},
    radius_A={"widget_type": "FloatSlider", "min": 0, "max": 10000},
    twist_deg={"widget_type": "FloatSlider", "min": 0, "max": 360},
    twist_offset={"widget_type": "FloatSlider", "min": 0, "max": 360},
)
def filament_particles(
    filament: napari.layers.Shapes,
    rise_A=50,
    twist_deg=0,
    twist_offset=0,
    radius_A=0,
    cyclic_symmetry_order=1,
) -> napari.types.LayerDataTuple:
    helical_filament = filament.metadata.get("helical_filament", None)
    if helical_filament is None:
        raise ValueError("This shapes layer contains no helical filament object.")

    exp_id = filament.metadata["experiment_id"]

    pos, ori = helical_filament.sample_helical(
        rise=rise_A / filament.scale[0],
        twist=twist_deg,
        radial_offset=radius_A / filament.scale[0],
        cyclic_symmetry_order=cyclic_symmetry_order,
        twist_offset=twist_offset,
        degrees=True,
    )

    features = pd.DataFrame({"orientation": np.asarray(Rotation.concatenate(ori))})

    return construct_particle_layer_tuples(
        coords=pos,
        features=features,
        scale=filament.scale[0],
        exp_id=exp_id,
        name_suffix="filament picked",
    )


@magicgui(
    labels=True,
    call_button="Resample",
    spacing_A={"widget_type": "FloatSlider", "min": 0.01, "max": 10000},
    thickness_A={"widget_type": "FloatSlider", "min": 0.01, "max": 10000},
)
def resample_filament(
    filament: napari.layers.Shapes,
    volume: napari.layers.Image,
    spacing_A=5,
    thickness_A=200,
) -> napari.types.LayerDataTuple:
    helical_filament = filament.metadata.get("helical_filament", None)
    if helical_filament is None:
        raise ValueError("This shapes layer contains no helical filament object.")

    exp_id = filament.metadata["experiment_id"]
    spacing = spacing_A / filament.scale[0]
    thickness = int(np.round(thickness_A / filament.scale[0]))

    vol = _resample_filament(volume, helical_filament, spacing, thickness)

    v = napari.Viewer()
    v.add_image(
        vol,
        name=f"{exp_id} - filament resampled",
        metadata={"experiment_id": exp_id, "stack": False},
        scale=filament.scale,
    )


@magicgui(
    labels=True,
    call_button="Generate",
)
def sphere(
    point_picks: napari.layers.Points,
) -> napari.types.LayerDataTuple:
    points = invert_xyz(point_picks.data)
    vert_all = []
    faces_all = []
    spheres_all = []

    exp_id = ""
    faces_offset = 0
    for i, (c, n) in enumerate(zip(points[::2], points[1::2])):
        r = np.linalg.norm(n - c)

        s = Sphere(center=c, radius=r)
        ps = PointSampler(spacing=r / 10)
        positions = ps.sample(s)

        h = ConvexHull(positions)
        tri = h.points[h.simplices]

        # fix faces ordering
        edges = tri - np.roll(tri, 1, axis=1)
        cross = np.cross(edges[:, 0], edges[:, 1])
        direction = np.einsum("...j,...j", cross, tri[:, 0] - (0, 0, 0))
        faces = h.simplices.copy()
        faces[direction < 0] = h.simplices[direction < 0][:, ::-1]
        faces += i * faces_offset
        faces_offset += len(positions)

        exp_id = point_picks.metadata["experiment_id"]
        vert_all.append(positions)
        faces_all.append(faces)
        spheres_all.append(s)

    surface_layer_tuple = (
        (invert_xyz(np.concatenate(vert_all)), np.concatenate(faces_all)),
        {
            "name": f"{exp_id} - surface",
            "metadata": {
                "experiment_id": exp_id,
                "spheres": spheres_all,
            },
            "scale": point_picks.scale,
            # smooth shading is bugged cause of some ordering issue
            "shading": "flat",
        },
        "surface",
    )
    return [surface_layer_tuple]


@magicgui(
    labels=True,
    call_button="Generate",
    spacing_A={"widget_type": "FloatSlider", "min": 0.01, "max": 10000},
)
def sphere_particles(
    sphere_surf: napari.layers.Surface,
    spacing_A=50,
) -> napari.types.LayerDataTuple:
    spheres = sphere_surf.metadata.get("spheres", None)
    if spheres is None:
        raise ValueError("This surface layer contains no sphere objects.")

    exp_id = sphere_surf.metadata["experiment_id"]

    spacing_A /= sphere_surf.scale[0]

    pos = []
    ori = []
    for s in spheres:
        ps = PoseSampler(spacing=spacing_A)
        poses = ps.sample(s)

        features = pd.DataFrame(
            {"orientation": np.asarray(Rotation.from_matrix(poses.orientations))}
        )
        pos.append(poses.positions)
        ori.append(features)

    return construct_particle_layer_tuples(
        coords=np.concatenate(pos),
        features=pd.concat(ori, axis=0),
        scale=sphere_surf.scale[0],
        exp_id=exp_id,
        name_suffix="spheres picked",
    )


class FilamentWidget(Container):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.append(filament)
        self.append(filament_particles)
        self.append(resample_filament)


class SurfaceWidget(Container):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.append(surface)
        self.append(surface_particles)
        self.append(resample_surface)


class SphereWidget(Container):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.append(sphere)
        self.append(sphere_particles)


@magic_factory(
    auto_call=True,
    rot={"widget_type": "Slider", "min": 0, "max": 360},
    tilt={"widget_type": "Slider", "min": 0, "max": 360},
    psi={"widget_type": "Slider", "min": 0, "max": 360},
)
def rotate_particles(
    particles: napari.layers.Points,
    rot: int = 0,
    tilt: int = 0,
    psi: int = 0,
) -> None:
    """Quick and dirty rotation setter based on RELION convention."""
    if particles.metadata.get("experiment_id", None) is None:
        raise ValueError("The selected layer is not a blik Particles layer.")
    ori = Rotation.from_euler("ZYZ", (rot, tilt, psi), degrees=True)
    particles.features.loc[list(particles.selected_data), "orientation"] = ori
    particles.events.features()
