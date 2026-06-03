output "elastic_ip" {
  description = "Public IPv4 address of the Elasticsearch VM."
  value = one([
    for a in ovh_cloud_project_instance.elastic.addresses : a.ip
    if a.version == 4
  ])
}

output "elastic_instance_id" {
  value = ovh_cloud_project_instance.elastic.id
}

output "elastic_volume_id" {
  value = ovh_cloud_project_volume.elastic_storage.id
}
