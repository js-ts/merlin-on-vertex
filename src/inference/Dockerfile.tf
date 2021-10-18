
FROM gcr.io/merlin-on-gcp/dongm-merlin-tf2:latest

# expose health and prediction listener ports from the image
EXPOSE 8000
EXPOSE 8001
EXPOSE 8002

# set working directory
WORKDIR /
RUN mkdir /model

# copy model files
COPY ./models/ /model/models/

ENV LD_LIBRARY_PATH /usr/local/cuda/lib:/usr/local/cuda/lib64:/usr/local/lib/x86_64-linux-gnu:/usr/lib/x86_64-linux-gnu:/usr/local/nvidia/lib:/usr/local/nvidia/lib64

# run Triton inference server (http server) to respond to prediction requests
CMD ["tritonserver", "--model-repository=/model/models/", "--backend-config=tensorflow,version=2"]

