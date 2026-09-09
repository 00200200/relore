{{- define "name" -}}
{{- default $.Release.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "app.name" -}}
ghlore
{{- end -}}

{{- define "labels.standard" -}}
release: {{ $.Release.Name | quote }}
heritage: {{ $.Release.Service | quote }}
chart: "{{ include "name" . }}"
app: "{{ include "app.name" . }}"
{{- end -}}

{{/*
The database URL every workload shares. Assembled here rather than in values so the
password comes from the Secret at runtime and never appears in a rendered manifest, a
values file, or `helm get values`. build-plan docs/SECRETS-equivalent: config is tracked,
credentials are not.
*/}}
{{- define "ghlore.databaseUrl" -}}
postgresql+psycopg://{{ .Values.postgres.user }}:$(POSTGRES_PASSWORD)@{{ include "name" . }}-postgres:5432/{{ .Values.postgres.database }}
{{- end -}}

{{/*
The verbosity flag, as argv entries. `-v` is a top-level flag on `ghlored`, so it
has to precede the subcommand -- `ghlored -v poll`, never `ghlored poll -v`.
*/}}
{{- define "ghlore.verbosity" -}}
{{- if eq (int .Values.verbosity) 1 }}"-v", {{ else if ge (int .Values.verbosity) 2 }}"-vv", {{ end }}
{{- end -}}
