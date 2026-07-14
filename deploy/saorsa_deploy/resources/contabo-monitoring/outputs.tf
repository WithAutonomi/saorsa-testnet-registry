output "monitoring_ip" {
  value = contabo_instance.monitoring.ip_config[0].v4[0].ip
}

output "grafana_ip" {
  value = var.enable_grafana ? contabo_instance.grafana[0].ip_config[0].v4[0].ip : ""
}

output "monitoring_instance_id" {
  value = contabo_instance.monitoring.id
}

output "grafana_instance_id" {
  value = var.enable_grafana ? contabo_instance.grafana[0].id : ""
}
