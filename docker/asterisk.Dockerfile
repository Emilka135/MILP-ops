FROM debian:bookworm-slim AS build
ARG ASTERISK_VERSION=22.11.0
ARG ASTERISK_SHA256=3bd5ee040509a3d3cd9b1ba9520c18e6ec0a7e7981ca68c457dcd36ba3c54d94
RUN apt-get update && apt-get install -y --no-install-recommends build-essential ca-certificates curl pkg-config libssl-dev libxml2-dev libncurses-dev uuid-dev libjansson-dev libsqlite3-dev libedit-dev python3 && rm -rf /var/lib/apt/lists/*
WORKDIR /build
RUN curl -fsSL "https://downloads.asterisk.org/pub/telephony/asterisk/asterisk-${ASTERISK_VERSION}.tar.gz" -o asterisk.tar.gz && \
    echo "${ASTERISK_SHA256}  asterisk.tar.gz" | sha256sum -c - && \
    tar xzf asterisk.tar.gz --strip-components=1 && rm asterisk.tar.gz
RUN ./configure --with-pjproject-bundled --without-dahdi --without-pri --without-unixodbc --disable-xmldoc && \
    make menuselect.makeopts && \
    menuselect/menuselect --disable BUILD_NATIVE --disable-category MENUSELECT_CORE_SOUNDS --disable-category MENUSELECT_EXTRA_SOUNDS --disable-category MENUSELECT_MOH menuselect.makeopts && \
    make -j2 && make DESTDIR=/opt/asterisk install && \
    cp COPYING /opt/asterisk/ASTERISK-COPYING
FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends python3 ca-certificates libssl3 libxml2 libncurses6 libuuid1 libjansson4 libsqlite3-0 libedit2 && rm -rf /var/lib/apt/lists/*
COPY --from=build /opt/asterisk/usr /usr
COPY --from=build /opt/asterisk/etc /etc
COPY --from=build /opt/asterisk/var/lib/asterisk /var/lib/asterisk
COPY --from=build /opt/asterisk/ASTERISK-COPYING /usr/share/doc/asterisk/COPYING
COPY telephony/asterisk /etc/asterisk
COPY telephony/entrypoint.py /usr/local/bin/clinic-asterisk
RUN useradd -u 10001 -m clinic && mkdir -p /var/run/asterisk /var/log/asterisk /var/spool/asterisk && chown -R clinic:clinic /etc/asterisk /var/lib/asterisk /var/run/asterisk /var/log/asterisk /var/spool/asterisk
USER clinic
CMD ["python3", "/usr/local/bin/clinic-asterisk"]
