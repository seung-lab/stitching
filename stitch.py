import argparse
import logging
from datetime import datetime
from time import time
import re

from typing import Mapping

import functools as ft
import jax
import jax.numpy as jnp
import numpy as np
from PIL import Image
from sofima import stitch_elastic, stitch_rigid, mesh, flow_utils, warp
import pandas as pd

def with_timer(func):
    def wrapper(*args, **kwargs):
        start_time = time()
        result = func(*args, **kwargs)
        end_time = time()
        elapsed_time = end_time - start_time
        print(f"{func.__name__} took {elapsed_time} seconds to execute.")
        return result
    return wrapper


def parse_section_path(section_path):
    # these are just for saving the output to a unique .npy file
    # parse reel, blade, and section from section_path using regex. this should probably aways work
    reel = re.search(r'reel(\d+)', section_path).group(1)
    blade = re.search(r'blade(\d+)', section_path).group(1)
    section = re.search(r's(\d+)', section_path).group(1)
    return reel, blade, section


def generate_supertile_map_from_csv(csv_path) -> np.ndarray:
    """
    Generate a 2D array of supertile_ids from the stage_positions.csv file
    """

    # Load the CSV file into a pandas DataFrame
    df = pd.read_csv(csv_path)

    # Correct the column names by removing leading spaces
    df.columns = [col.strip() for col in df.columns]

    # Normalize the stage_x_nm and stage_y_nm values
    df["norm_stage_x"] = df["stage_x_nm"].rank(method="dense").astype(int) - 1
    df["norm_stage_y"] = df["stage_y_nm"].rank(method="dense").astype(int) - 1

    # Determine the dimensions of the 2D array
    max_x = df["norm_stage_x"].max()
    max_y = df["norm_stage_y"].max()

    # Initialize an array of shape (max_y+1, max_x+1) with None values
    arr = np.full((max_y + 1, max_x + 1), None)

    # Populate the array with tile_id values using numpy's advanced indexing
    arr[df["norm_stage_y"].values, df["norm_stage_x"].values] = df["tile_id"].values

    # Reverse the order of the rows in the array
    arr_reversed = arr[::-1]

    return arr_reversed

def generate_tile_id_map(supertile_map) -> np.ndarray:
    """
    Generate a 2D array of tile_ids from the supertile_map
    """

    # Cricket subtile order
    SUBTILE_MAP = [[6, 7, 8], [5, 0, 1], [4, 3, 2]]

    tile_id_map = []
    for supertile_row in supertile_map:
        for subtile_row in SUBTILE_MAP:
            current_row = []
            for supertile in supertile_row:
                for subtile in subtile_row:
                    if supertile is None:
                        current_row.append(None)
                    else:
                        current_row.append(f"{supertile:04}_{subtile}")
            tile_id_map.append(current_row)
    return np.array(tile_id_map)

@with_timer
def load_tiles(tile_id_map, path_to_section: str) -> Mapping[tuple[int, int], np.ndarray]:
    """Load the tiles from disk and return a map of tile_id -> tile_image"""
    print("Loading tiles from disk")
    tile_map = {}
    for y in range(tile_id_map.shape[0]):
        for x in range(tile_id_map.shape[1]):
            tile_id = tile_id_map[y, x]
            if tile_id is None:
                continue
            # print(f"Loading {tile_id}")
            with open(f"{path_to_section}/subtiles/tile_{tile_id}.bmp", "rb") as fp:
                img = Image.open(fp)
                tile_map[(x, y)] = np.array(img)

    return tile_map

@with_timer
def compute_coarse_tile_positions(
    tile_space, tile_map, overlaps_xy=((1000, 1000), (1500, 1500))
):
    print("Computing coarse tile positions")

    coarse_offsets_x, coarse_offsets_y = stitch_rigid.compute_coarse_offsets(
        tile_space, tile_map, overlaps_xy
    )

    if np.inf in coarse_offsets_x:
        coarse_offsets_x = stitch_rigid.interpolate_missing_offsets(coarse_offsets_x, -1)
    if np.inf in coarse_offsets_y:
        coarse_offsets_y = stitch_rigid.interpolate_missing_offsets(coarse_offsets_y, -2)

    print("optimize_coarse_mesh")

    coarse_mesh = stitch_rigid.optimize_coarse_mesh(
        coarse_offsets_x, coarse_offsets_y
    )

    return np.squeeze(coarse_offsets_x), np.squeeze(coarse_offsets_y), coarse_mesh


def cleanup_flow_fields(fine_x, fine_y):
    print("Cleaning up flow fields")
    kwargs = {
        "min_peak_ratio": 1.4,
        "min_peak_sharpness": 1.4,
        "max_deviation": 5,
        "max_magnitude": 0,
    }
    fine_x = {
        k: flow_utils.clean_flow(v[:, np.newaxis, ...], **kwargs)[:, 0, :, :]
        for k, v in fine_x.items()
    }
    fine_y = {
        k: flow_utils.clean_flow(v[:, np.newaxis, ...], **kwargs)[:, 0, :, :]
        for k, v in fine_y.items()
    }

    kwargs = {"min_patch_size": 10, "max_gradient": -1, "max_deviation": -1}
    fine_x = {
        k: flow_utils.reconcile_flows([v[:, np.newaxis, ...]], **kwargs)[
            :, 0, :, :
        ]
        for k, v in fine_x.items()
    }
    fine_y = {
        k: flow_utils.reconcile_flows([v[:, np.newaxis, ...]], **kwargs)[
            :, 0, :, :
        ]
        for k, v in fine_y.items()
    }
    return fine_x, fine_y

@with_timer
def compute_flow_maps(coarse_offsets_x, coarse_offsets_y, tile_map, stride):
    # The stride (in pixels) specifies the resolution at which to compute the flow
    # fields between tile pairs. This is the same as the resolution at which the
    # mesh is later optimized. The more deformed the tiles initially are, the lower
    # the stride needs to be to get good stitching results.

    print("Computing flow maps")

    print("compute_flow_map x")
    fine_x, offsets_x = stitch_elastic.compute_flow_map(
        tile_map, coarse_offsets_x, 0, stride=(stride, stride), batch_size=4
    )

    print("compute_flow_map y")
    fine_y, offsets_y = stitch_elastic.compute_flow_map(
        tile_map, coarse_offsets_y, 1, stride=(stride, stride), batch_size=4
    )

    # Clean up the flow fields.
    fine_x, fine_y = cleanup_flow_fields(fine_x, fine_y)

    return fine_x, fine_y, offsets_x, offsets_y


@with_timer
def run_mesh_solver(coarse_offsets_x, coarse_offsets_y, coarse_mesh, fine_x, fine_y, offsets_x, offsets_y, tile_map, stride):
    print("Preparing data for mesh solver")

    fx, fy, x, nbors, key_to_idx = stitch_elastic.aggregate_arrays(
        (coarse_offsets_x, fine_x, offsets_x),
        (coarse_offsets_y, fine_y, offsets_y),
        list(tile_map.keys()),
        coarse_mesh[:, 0, ...],
        stride=(stride, stride),
        tile_shape=next(iter(tile_map.values())).shape,
    )

    @jax.jit
    def prev_fn(x):
        target_fn = ft.partial(
            stitch_elastic.compute_target_mesh,
            x=x,
            fx=fx,
            fy=fy,
            stride=(stride, stride),
        )
        x = jax.vmap(target_fn)(nbors)
        return jnp.transpose(x, [1, 0, 2, 3])

    # These detault settings are expect to work well in most configurations. Perhaps
    # the most salient parameter is the elasticity ratio k0 / k. The larger it gets,
    # the more the tiles will be allowed to deform to match their neighbors (in which
    # case you might want use aggressive flow filtering to ensure that there are no
    # inaccurate flow vectors). Lower ratios will reduce deformation, which, depending
    # on the initial state of the tiles, might result in visible seams.
    mesh_integration_config = mesh.IntegrationConfig(
        dt=0.001,
        gamma=0.0,
        k0=0.01,
        k=0.1,
        stride=stride,
        num_iters=1000,
        max_iters=20000,
        stop_v_max=0.001,
        dt_max=100,
        prefer_orig_order=True,
        start_cap=0.1,
        final_cap=10.0,
        remove_drift=True,
    )

    print("Running mesh solver")
    x, ekin, t = mesh.relax_mesh(
        x, None, mesh_integration_config, prev_fn=prev_fn   
    )

    # Unpack meshes into a dictionary.
    idx_to_key = {v: k for k, v in key_to_idx.items()}
    meshes = {idx_to_key[i]: np.array(x[:, i : i + 1 :, :]) for i in range(x.shape[1])}

    return meshes

@with_timer
def warp_and_render_tiles(tile_map, meshes, stride):
    print("Warping and rendering the stitched tiles")
    stitched, mask = warp.render_tiles(
        tile_map, meshes, stride=(stride, stride), parallelism=32
    )
    return stitched, mask




## Main

def main(section_path: str, first_x: int, first_y: int, d_x: int, d_y: int):
    ## Job config

    reel, blade, section = parse_section_path(section_path)
    datestamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    
    subset_string = ""
    if d_x is not None and d_y is not None:
        subset_string = f"_subset_x{first_x}_y{first_y}_dx{d_x}_dy{d_y}"
    
    stitched_filename = f"reel{reel}_blade{blade}_s{section}_{datestamp}{subset_string}_stitched.npy"

    # Resolution for flow field computation and mesh optimization.
    STRIDE = 20

    # Get the supertile map and generate the tile_id_map from stage_positions.csv
    supertile_map = generate_supertile_map_from_csv(f"{section_path}/metadata/stage_positions.csv")
 
    if d_x is not None and d_y is not None:
        supertile_map = supertile_map[first_x : first_x + d_x, first_y : first_y + d_y]
    tile_id_map = generate_tile_id_map(supertile_map)


    ## These are the 5 time-consuming steps of the stitching process

    # 1) Load the tiles from disk
    tile_map = load_tiles(tile_id_map, section_path)

    # 2) Compute the coarse tile positions and the initial mesh.
    coarse_offsets_x, coarse_offsets_y, coarse_mesh = compute_coarse_tile_positions(
        tile_space=tile_id_map.shape, tile_map=tile_map
    )

    # 3) Compute the flow maps between tile pairs.
    fine_x, fine_y, offsets_x, offsets_y = compute_flow_maps(
        coarse_offsets_x, coarse_offsets_y, tile_map, STRIDE
    )

    # 4) Run the mesh solver.
    meshes = run_mesh_solver(
        coarse_offsets_x,
        coarse_offsets_y,
        coarse_mesh,
        fine_x,
        fine_y,
        offsets_x,
        offsets_y,
        tile_map,
        STRIDE,
    )

    # 5) Warp the tiles into a single image.
    stitched, mask = warp_and_render_tiles(tile_map, meshes, STRIDE)

    # Save the section to disk
    print("Saving the stiched section to disk")
    np.save(stitched_filename, stitched)
    # TODO: save the mask to disk as well, 
    # TODO: upload to neuroglancer w/ cloudvolume

    print("Stitching completed successfully and result saved.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Stitch a section from disk')
    parser.add_argument('section_path', type=str, help='Path to the section to stitch')
    parser.add_argument('--first_x', type=int, default=0, help='First x position of the supertile subset')
    parser.add_argument('--first_y', type=int, default=0, help='First y position of the supertile subset')
    parser.add_argument('--d_x', type=int, help='Width of the supertile subset')
    parser.add_argument('--d_y', type=int, help='Height of the supertile subset')
    
    args = parser.parse_args()
    
    section_path = args.section_path
    first_x = args.first_x
    first_y = args.first_y
    d_x = args.d_x
    d_y = args.d_y
    
    print(f"Stitching section at {section_path}")
    
    total_start = time()
    main(section_path, first_x, first_y, d_x, d_y)
    total_end = time()
    print(f"Total time elapsed: {total_end - total_start} seconds.")
