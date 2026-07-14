output "elastic_ip" {
  value = contabo_instance.elastic.ip_config[0].v4[0].ip
}

output "elastic_instance_id" {
  value = contabo_instance.elastic.id
}
