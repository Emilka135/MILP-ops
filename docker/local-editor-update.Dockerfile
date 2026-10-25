# Offline update using the spreadsheet packages already installed in our CRM image.
ARG DEPENDENCY_IMAGE=clinic-voice-crm
ARG BASE_IMAGE=clinic-voice-local-base:scenario-editor
FROM ${DEPENDENCY_IMAGE} AS spreadsheet
FROM ${BASE_IMAGE}
COPY --from=spreadsheet /usr/local/lib/python3.12/site-packages/openpyxl /opt/clinic-source-deps/openpyxl
COPY --from=spreadsheet /usr/local/lib/python3.12/site-packages/openpyxl-3.1.5.dist-info /opt/clinic-source-deps/openpyxl-3.1.5.dist-info
COPY --from=spreadsheet /usr/local/lib/python3.12/site-packages/et_xmlfile /opt/clinic-source-deps/et_xmlfile
COPY --from=spreadsheet /usr/local/lib/python3.12/site-packages/et_xmlfile-2.0.0.dist-info /opt/clinic-source-deps/et_xmlfile-2.0.0.dist-info
ENV PYTHONPATH=/opt/clinic-source-deps:/workspace/runtime/src
COPY runtime/src /workspace/runtime/src
COPY app/__init__.py app/types.py /workspace/app/
COPY app/mis /workspace/app/mis
COPY scenario-editor/dist /workspace/scenario-editor/dist

COPY scenarios /workspace/scenarios

COPY reference-data /workspace/reference-data
