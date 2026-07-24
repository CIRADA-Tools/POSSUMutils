"""
These tests require a local Possum REST API server running connected with a copy of database set up locally.
You also need to run a local Prefect server with the following secrets:
- possum-api-url
- possum-api-username
- possum-api-password
Alteratively you can create a config.env in automation folder with the following keys (and values):
POSSUM_API_USERNAME=
POSSUM_API_PASSWORD=
POSSUM_API_URL=
There are no assertions because it will depend on the database copy.
You will need to observe the output and compare against the database.
"""
from datetime import date
from automation import possum_api_client as rest_api

band_number = 1
tile_number = 11315
status = "COMPLETED"
        
def test_update_1d_pipeline_validation():        
    full_field_name = "EMU_1058-60"
    status = "test"
    conn = rest_api.PossumApiClient() # pass in config.env if using config.env instead of Prefect secrets
    response = conn.patch("/1d-pipeline/observations/update/1d_pipeline_validation/?"
                      f"band_number={band_number}&"
                      f"field_name={full_field_name}&"
                      f"status={status}")
    rows_to_update = response.get("rows_updated")
    print('Rows updated:', rows_to_update)

def test_update_single_sb_1d_pipeline():     
    full_field_name = "EMU_1058-60"
    status_column = "single_SB_1D_pipeline"
    conn = rest_api.PossumApiClient() # pass in config.env if using config.env instead of Prefect secrets
    response = conn.patch(f"/1d-pipeline/observations/update/{status_column.lower()}/?"
                      f"band_number={band_number}&"
                      f"field_name={full_field_name}&"
                      f"status={status}")
    rows_to_update = response.get("rows_updated")
    print('Rows updated:', rows_to_update)

def test_update_partial_tiles():
    full_field_name = "EMU_1058-60"
    conn = rest_api.PossumApiClient() # pass in config.env if using config.env instead of Prefect secrets 
    response = conn.patch("/1d-pipeline/partial-tiles/update/status/",
                        json={
                            "band_number": band_number,
                            "field_name": full_field_name,
                            "tile_numbers": [tile_number, '', '', ''],
                            "status": status,
                        },
    )
    rows_to_update = response.get("rows_updated")
    print('Rows updated:', rows_to_update)

def test_update_3d_pipeline_val():
    conn = rest_api.PossumApiClient() # pass in config.env if using config.env instead of Prefect secrets 
    response = conn.patch(f"/3d-pipeline/tiles/update/3d_pipeline_val/?band_number={band_number}"
                      f"&tile_number={tile_number}"
                      f"&3d_pipeline_val={status}")
    rows_updated = response.get('rows_updated')
    print('Rows updated:', rows_updated)

def test_update_3d_val_link():    
    conn = rest_api.PossumApiClient() # pass in config.env if using config.env instead of Prefect secrets 
    response = conn.patch(f"/3d-pipeline/tiles/update/3d_val_link/?band_number={band_number}"
               f"&tile_number={tile_number}"
               f"&3d_val_link='test'")
    rows_updated = response.get('rows_updated')
    print('Rows updated:', rows_updated)

def test_update_3d_pipeline():
    conn = rest_api.PossumApiClient() # pass in config.env if using config.env instead of Prefect secrets
    today = date.today().isoformat()
    response = conn.patch(f"/3d-pipeline/tiles/update/3d_pipeline/?band_number={band_number}",
                    json={
                          "band_number": 1,
                          "tile_number": tile_number,
                          "timestamp": today
                    }, #avoid encoding problem in url
    )
    print('Rows updated:', response.get("rows_updated"))    

def test_tiles_ready_for_3d_pipeline():
    conn = rest_api.PossumApiClient() # pass in config.env if using config.env instead of Prefect secrets
    tile_numbers = conn.get("/3d-pipeline/tiles/ready-for-3d/band1/")
    # tile_numbers is a list of single-element tuples, convert to 1D list
    tile_numbers = [str(tup[0]) for tup in tile_numbers]
    print(tile_numbers)

def test_tiles_ready_for_ingest():
    conn = rest_api.PossumApiClient() # pass in config.env if using config.env instead of Prefect secrets
    tile_numbers = conn.get_json(f"/3d-pipeline/tiles/ready-for-ingest/band{band_number}/")
    print(tile_numbers)

def test_3d_plotting():
    conn = rest_api.PossumApiClient() # pass in config.env if using config.env instead of Prefect secrets
    rows = conn.get("/3d-pipeline/tiles/plotting/band1/")
    # tile, "3d_pipeline_val", "3d_val_link", "3d_pipeline_ingest", "3d_pipeline", "cube_state"
    print(rows)

def test_tiles_by_tile_id():
    conn = rest_api.PossumApiClient() # pass in config.env if using config.env instead of Prefect secrets
    rows = conn.get_json(f"/3d-pipeline/tiles/tile-id/band{band_number}/{tile_number}/")
    print(rows)
    if len(rows) > 0:
        ingest_value = rows[0]["3d_pipeline_ingest"]
        print('ingest value ', ingest_value)    

if __name__ == "__main__":
    test_update_1d_pipeline_validation()
    test_update_single_sb_1d_pipeline()
    test_update_partial_tiles()
    test_update_3d_pipeline_val()
    test_update_3d_val_link()
    test_update_3d_pipeline()
    test_tiles_ready_for_3d_pipeline()
    test_tiles_ready_for_ingest()
    test_3d_plotting()
    test_tiles_by_tile_id()        