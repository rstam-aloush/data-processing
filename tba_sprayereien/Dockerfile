FROM python:3.11-bullseye
WORKDIR /code/data-processing
#ARG HTTPS_PROXY
#ENV HTTPS_PROXY=$HTTPS_PROXY
RUN python3 -m pip install --user --no-cache-dir pandas==2.2.0
RUN python3 -m pip install --user --no-cache-dir geopandas==1.0.1
RUN python3 -m pip install --user --no-cache-dir requests==2.31.0
RUN python3 -m pip install --user --no-cache-dir filehash==0.2.dev1
RUN python3 -m pip install --user --no-cache-dir more-itertools==10.2.0
CMD ["python3", "-m", "tba_sprayereien.etl"]


# Docker commands to create image and run container:
# cd tba_sprayereien
# docker build -t tba_sprayereien .
# cd ..
# docker run -it --rm -v /data/dev/workspace/data-processing:/code/data-processing --name tba_sprayereien tba_sprayereien
