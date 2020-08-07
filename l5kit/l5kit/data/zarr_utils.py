import os
from collections import Counter
from pathlib import Path
from typing import List, Optional

import numpy as np
from tqdm import tqdm

from . import ChunkedDataset

GIGABYTE = 1 * 1024 * 1024 * 1024


def _compute_path_size(path: str) -> int:
    """
    Compute the total size of the folder, considering also nested elements.
    Can be run to get zarr total size

    Args:
        path (str): base path

    Returns:
        (int): total size in bytes
    """
    root_directory = Path(path)
    return sum(f.stat().st_size for f in root_directory.glob("**/*") if f.is_file())


def _get_num_els_in_scene_range(zarr_dataset: ChunkedDataset, scene_index_start: int, scene_index_end: int) -> dict:
    """
    Get numbers of scenes, frames, agents, tl_lights in a set of scene in a zarr
    Args:
        zarr_dataset (ChunkedDataset): zarr dataset to use for computing number of elements
        scene_index_start (int): start from this scene (included)
        scene_index_end (int): end before this scene (excluded!!)

    Returns:
        dict: a dict with keys equal to zarr initialise args
    """
    assert scene_index_end > scene_index_start

    scene_a = zarr_dataset.scenes[scene_index_start]
    scene_b = zarr_dataset.scenes[scene_index_end - 1]

    frame_a = zarr_dataset.frames[scene_a["frame_index_interval"][0]]
    frame_b = zarr_dataset.frames[scene_b["frame_index_interval"][1] - 1]

    return {
        "num_scenes": scene_index_end - scene_index_start,
        "num_frames": scene_b["frame_index_interval"][1] - scene_a["frame_index_interval"][0],
        "num_agents": frame_b["agent_index_interval"][1] - frame_a["agent_index_interval"][0],
        "num_tl_faces": frame_b["traffic_light_faces_index_interval"][1]
        - frame_a["traffic_light_faces_index_interval"][0],
    }


def _append_zarr_subset(
    input_zarr: ChunkedDataset,
    output_zarr: ChunkedDataset,
    scene_index_start: int,
    scene_index_end: int,
    output_zarr_num_els: Optional[dict] = None,
) -> None:
    """
    Append a subset of input_zarr into output_zarr. To avoid appending (slow), output_zarr must be opened in write mode
    and with pre-allocated shape already. End indices of output_zarr are read from output_zarr_num_els, or 0 is assumed
    otherwise

    Args:
        input_zarr (ChunkedDataset): origin zarr in read mode
        output_zarr (ChunkedDataset): zarr already opened in write mode and with pre-allocated arrays
        scene_index_start (int): index of the first scene to copy
        scene_index_end (int): index of the last scene (excluded)
        output_zarr_num_els (Optional[dict]): if None, write starting from 0 index in the output zarr

    Returns:

    """

    # start indices in the destination zarr
    if output_zarr_num_els is None:
        idx_output_scene = 0
        idx_output_frame = 0
        idx_output_agent = 0
        idx_output_tl_face = 0
    else:
        idx_output_scene = output_zarr_num_els["num_scenes"]
        idx_output_frame = output_zarr_num_els["num_frames"]
        idx_output_agent = output_zarr_num_els["num_agents"]
        idx_output_tl_face = output_zarr_num_els["num_tl_faces"]

    # relative indices to subtract before copying to erase input history
    idx_start_frame = input_zarr.scenes[scene_index_start]["frame_index_interval"][0]
    idx_start_agent = input_zarr.frames[idx_start_frame]["agent_index_interval"][0]
    idx_start_tl_face = input_zarr.frames[idx_start_frame]["traffic_light_faces_index_interval"][0]

    for idx_scene in range(scene_index_start, scene_index_end):
        # get slices from input zarr
        scenes = input_zarr.scenes[idx_scene : idx_scene + 1]
        frames = input_zarr.frames[slice(*scenes[0]["frame_index_interval"])]
        agents = input_zarr.agents[slice(frames[0]["agent_index_interval"][0], frames[-1]["agent_index_interval"][1])]
        tl_faces = input_zarr.tl_faces[
            slice(
                frames[0]["traffic_light_faces_index_interval"][0], frames[-1]["traffic_light_faces_index_interval"][1]
            )
        ]

        # subtract idx_start (remove history input_zarr) and sum idx_output (add history output_zarr)
        scenes["frame_index_interval"] += idx_output_frame - idx_start_frame
        frames["agent_index_interval"] += idx_output_agent - idx_start_agent
        frames["traffic_light_faces_index_interval"] += idx_output_tl_face - idx_start_tl_face

        # copy from input_zarr to output_zarr
        output_zarr.scenes[idx_output_scene : idx_output_scene + len(scenes)] = scenes
        output_zarr.frames[idx_output_frame : idx_output_frame + len(frames)] = frames
        output_zarr.agents[idx_output_agent : idx_output_agent + len(agents)] = agents
        output_zarr.tl_faces[idx_output_tl_face : idx_output_tl_face + len(tl_faces)] = tl_faces

        # update output indices
        idx_output_scene += len(scenes)
        idx_output_frame += len(frames)
        idx_output_agent += len(agents)
        idx_output_tl_face += len(tl_faces)


# TODO add testing
def zarr_concat(input_zarrs: List[str], output_zarr: str) -> None:
    """
    Concat many zarr into a single one. Takes care of updating indices for frames and agents.

    Args:
        input_zarrs (List[str]): a list of paths to input zarrs
        output_zarr (str): the path to the output zarr

    Returns:

    """

    assert not os.path.exists(output_zarr), "we need to pre-allocate zarr, can't append fast"
    output_dataset = ChunkedDataset(output_zarr)

    # we need to estimate how much to allocate first by reading all input zarrs lens
    num_els_inputs_zarrs = {}
    for input_zarr in input_zarrs:
        input_dataset = ChunkedDataset(input_zarr)
        input_dataset.open()
        num_els_inputs_zarrs[input_zarr] = _get_num_els_in_scene_range(input_dataset, 0, len(input_dataset.scenes))

    # we can now pre-allocate the output dataset
    total_num_els: Counter = Counter()
    for num_el in num_els_inputs_zarrs.values():
        total_num_els += Counter(num_el)
    output_dataset.initialize(**total_num_els)

    cur_num_els = Counter({"num_scenes": 0, "num_frames": 0, "num_agents": 0, "num_tl_faces": 0})
    tqdm_bar = tqdm(input_zarrs)
    for input_zarr in tqdm_bar:
        tqdm_bar.set_description(f"working on {input_zarr}")

        input_dataset = ChunkedDataset(input_zarr)
        input_dataset.open()

        _append_zarr_subset(input_dataset, output_dataset, 0, len(input_dataset.scenes), cur_num_els)
        cur_num_els += Counter(num_els_inputs_zarrs[input_zarr])

        # TODO move into tests
        assert np.all(output_dataset.agents[cur_num_els["num_agents"] - 1] == input_dataset.agents[-1])
        if len(input_dataset.tl_faces) > 0:
            assert np.all(output_dataset.tl_faces[cur_num_els["num_tl_faces"] - 1] == input_dataset.tl_faces[-1])


def zarr_split(input_zarr: str, output_zarr_1: str, output_zarr_2: str, size_output_zarr_1_gb: float) -> None:
    """
    Split the input zarr into two zarrs. The first one (zarr_1) will be cut from the left side and will have size
    size_output_zarr_1_gb. The rest of the zarr will end in zarr_2.
    The assumption here is that scenes have roughly the same size.

    If zarr is 20GB and size_output_zarr_1_gb is 5Gb then:
    zarr_1 -> first 5GB of zarr
    zarr_2 -> last 15GB of zarr

    Args:
        input_zarr (str): path of the original zarr
        output_zarr_1 (str): path to the first output zarr
        output_zarr_2 (str): path to second output zarr
        size_output_zarr_1_gb (float): size of the first output zarr

    Returns:

    """
    input_dataset = ChunkedDataset(input_zarr)
    input_dataset.open()

    # compute the size of the input_dataset in GB and check if the provided one for the cut is lower
    size_input_zarr_gb = _compute_path_size(input_zarr) / GIGABYTE
    assert (
        size_output_zarr_1_gb < size_input_zarr_gb
    ), f"input size: {size_input_zarr_gb} smaller than {size_output_zarr_1_gb}"

    # convert gb size in number of scenes (assumption: scene have the same size)
    num_scenes_input_zarr = len(input_dataset.scenes)
    num_scenes_output_zarr_1 = int(num_scenes_input_zarr * size_output_zarr_1_gb / size_input_zarr_gb)
    num_scenes_output_zarr_2 = num_scenes_input_zarr - num_scenes_output_zarr_1
    assert num_scenes_output_zarr_2 > 0

    # instead of appending in the output zarrs, we can pre-allocate and assign (faster)
    num_els_output_zarr_1 = _get_num_els_in_scene_range(input_dataset, 0, num_scenes_output_zarr_1)
    num_els_output_zarr_2 = _get_num_els_in_scene_range(input_dataset, num_scenes_output_zarr_1, num_scenes_input_zarr)

    output_dataset_1 = ChunkedDataset(output_zarr_1)
    output_dataset_1.initialize(**num_els_output_zarr_1)

    output_dataset_2 = ChunkedDataset(output_zarr_2)
    output_dataset_2.initialize(**num_els_output_zarr_2)

    _append_zarr_subset(input_dataset, output_dataset_1, 0, num_scenes_output_zarr_1)
    _append_zarr_subset(input_dataset, output_dataset_2, num_scenes_output_zarr_1, num_scenes_input_zarr)

    # safety checks TODO move to test (but artefacts there are very very tiny)
    output_dataset_1 = ChunkedDataset(output_zarr_1)
    output_dataset_1.open()

    assert np.all(output_dataset_1.scenes[0] == input_dataset.scenes[0])
    assert np.all(output_dataset_1.frames[0] == input_dataset.frames[0])
    assert np.all(output_dataset_1.agents[0] == input_dataset.agents[0])
    assert np.all(output_dataset_1.tl_faces[0] == input_dataset.tl_faces[0])

    output_dataset_2 = ChunkedDataset(output_zarr_2)
    output_dataset_2.open()

    # can't check easily frames and scene as interval_index are different
    scene = input_dataset.scenes[num_scenes_output_zarr_1]
    frame = input_dataset.frames[scene["frame_index_interval"][0]]
    assert np.all(output_dataset_2.agents[0] == input_dataset.agents[frame["agent_index_interval"][0]])
    assert np.all(
        output_dataset_2.tl_faces[0] == input_dataset.tl_faces[frame["traffic_light_faces_index_interval"][0]]
    )

    assert np.all(output_dataset_2.agents[-1] == input_dataset.agents[-1])
    assert np.all(output_dataset_2.tl_faces[-1] == input_dataset.tl_faces[-1])