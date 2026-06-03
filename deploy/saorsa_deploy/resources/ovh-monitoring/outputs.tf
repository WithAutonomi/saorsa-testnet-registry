output "monitoring_ip" {
  description = "Public IPv4 address of the InfluxDB VM."
  value = one([
    for a in ovh_cloud_project_instance.monitoring.addresses : a.ip
    if a.version == 4
  ])
}

output "grafana_ip" {
  description = "Public IPv4 address of the Grafana VM."
  value = one([
    for a in ovh_cloud_project_instance.grafana.addresses : a.ip
    if a.version == 4
  ])
}

output "monitoring_instance_id" {
  value = ovh_cloud_project_instance.monitoring.id
}

output "grafana_instance_id" {
  value = ovh_cloud_project_instance.grafana.id
}

output "monitoring_volume_id" {
  value = ovh_cloud_project_volume.monitoring_storage.id
}
