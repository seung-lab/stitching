# How to stitch with FEABAS (WIP)

Right now this is a mess and there are many manual steps still. And this list likely has some errors/missing steps. Double check for hardcoded paths, etc.

0. Download FEABAS source from [github](https://github.com/YuelongWu/feabas), into say, `~/src/feabas`. Scripts currently expect this location. 
1. Create a python venv for feabas. Scripts currently expect this location.
    - `module load anacondapy/2023.03`
    - `python -m venv ~/feabas-venv`
    - `source ~/feabas-venv/bin/activate`
    - `pip install feabas`
3. Construct FEABAS working directory and some config. This is where intermediate and final results will be saved. Other dirs here should be created and populated by feabas:
    - / feabas working dir, for example i am using `/scratch/rmorey/feabas-mec-stitching`
        - /configs
            - stitching_configs.yaml (check the example)
        - /stitch
            - /stitch_coord - we will populate this in step 4
4. Go to your feabas source, and modify `configs/general_configs.yaml` to point at your feabas working dir
5. Generate stitch coords file for each section from stage positions
- use `gen_stitch_coord.py` with the path to your section directory, e.g. `/scratch/yannan/TEM/bladeseq-2024.06.26-16.12.30/s017-2024.06.26-16.12.30`. By default this will store the coords files to `./stitch_coords` but you should modify the script to point this toward `stitch_coord` in your feabas working dir, or copy the ones you want into there.

    ```
    python gen_stitch_coord.py /scratch/yannan/TEM/bladeseq-2024.06.26-16.12.30/s017-2024.06.26-16.12.30
    ```
    I have been using a script like `gen_coords.sh` to do all these.

5. Launch stitching jobs
    - option 1: launch all in one job. If you launch a slurm job that looks like `feabas.slurm` feabas will look at your working dir stitch_coords, and sequentially stitch all the sections there on one node.
    - option 2: multiple sections per node: this is less fleshed out, but I have a script in `launch_feabas.sh` that is meant to launch a single job on a node for each section in the stitch coords, using a start/stop/interval argument from feabas. Note that the numbers in the array at the top of that file are indicies into stitch_coords, not actual section numbers. To do the whole dataset we will need to be more sophisticated about distributing jobs, this is a simple one job per node script right now. 
    - Stuff should start populating your feabas working dir, including logs, intermediate results, and hopefully the stitched sections in neuroglancer format.
6. Copy the stitched sections to cloudian, with rclone or cloudfiles
7. Downsample stitched sections with igneous
