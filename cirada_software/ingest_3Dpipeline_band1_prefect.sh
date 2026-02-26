echo "Preparing pipeline ingest for tile number $1 band $2"

# automatically export all variables for Prefect
set -a
source /arc/projects/CIRADA/polarimetry/software/POSSUMutils/automation/config.env
set +a

#RMtools not used but cant hurt to add it to path
echo "TEMPORARILY: adding RMtools[dev] to pythonpath until new release (>v1.4.6)"
export PYTHONPATH="/arc/projects/CIRADA/polarimetry/software/RMtoolsdev/:$PYTHONPATH"

# move to POSSUMutils base directory
cd /arc/projects/CIRADA/polarimetry/software/POSSUMutils/
# arguments: tile_number, band ("943MHz" or "1367MHz")
python -m possum_pipeline_control.ingest3Dpipeline $1 $2 -CADC_cert_file $HOME/.ssl/cadcproxy.pem