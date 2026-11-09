FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl gnupg \
    && curl --fail --location https://dl.openfoam.org/gpg.key -o /usr/share/keyrings/openfoam.asc \
    && printf '%s\n' 'deb [signed-by=/usr/share/keyrings/openfoam.asc] http://dl.openfoam.org/ubuntu noble main' > /etc/apt/sources.list.d/openfoam.list \
    && apt-get update && apt-get install -y --no-install-recommends openfoam14 \
    && rm -rf /var/lib/apt/lists/*
ENV FOAM_BASHRC=/opt/openfoam14/etc/bashrc
WORKDIR /case
CMD ["bash", "-c", ". /opt/openfoam14/etc/bashrc && foamRun -help"]
