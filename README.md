# POSSUMutils
A set of control tools related to running the POSSUM 1D and 3D pipelines on the CANFAR science platform. 

Please see the documentation "Possum Pipeline Documentation" at https://askap.org/possum/Data/POSSUMPipelineControlDocs

With everything set up (credentials, etc) on CANFAR and the AUSSRC Prefect server, start running the 3D / 1D pipeline from deployments at

https://possum-prefect.aussrc.org/deployments

High-level overview of the 1D pipeline (updated on 2026-02-20)
<img width="4402" height="1886" alt="POSSUM_1d_PartialTiles_v2 light" src="https://github.com/user-attachments/assets/43e676ab-3c5a-4d91-a2a1-0dedca39285f" />

High-level overview of the 3D pipeline (updated on 2026-02-20)
<img width="4112" height="2048" alt="POSSUM_3d_pipeline_control_v2 light" src="https://github.com/user-attachments/assets/c15b30fc-22ba-4374-a940-bbd688ce3bae" />



## cirada_software/

A set of scripts meant to live on CANFAR where they can be called to start headless jobs.


## possum_pipeline_control/

A set of scripts meant to live on the AUSSRC Prefect VM to operate the control logic for launching POSSUM pipelines on CANFAR. Particularly the control*.py files. 


## root directory (./)
For quick looks at the POSSUM processing status or data availability.

### Grab closest POSSUM field and tile
```get_POSSUM_field_sbid_and_tile.py``` 

Query POSSUM status sheet for some target (name) or RA,DEC. Returns the closest field and tile, whether they've been processed, and direct download links to the data (requires CANFAR login). 

Example usage:

`python get_POSSUM_field_sbid_and_tile.py -t "Abell 3627"`

or

`python get_POSSUM_field_sbid_and_tile.py --coords 246.85 -60.32`


### Query POSSUM status
```query_status.py``` 

Query POSSUM status page maintained by Cameron van Eck for some target RA,DEC

Simply scrapes the data from the HTML source page: 
view-source:https://www.mso.anu.edu.au/~cvaneck/possum/aladin_survey_band1.html
and checks whether coordinates fall inside one of the field with a certain status.

Assumes flat sky, so might not work for sources on boundary of observations? Probably superseded by `get_POSSUM_field_sbid_and_tile.py`


## handy_scripts/

Some handy scripts not necessarily related to POSSUM.



## TODO:

- Should move to dev/test/prod env for the pipelines
- Should implement CANDIAPL best practices, and make this a proper python package.

