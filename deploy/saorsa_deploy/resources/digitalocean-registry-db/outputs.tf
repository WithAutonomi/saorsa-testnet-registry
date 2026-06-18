output "cluster_name" {
  description = "Name of the managed Postgres cluster."
  value       = digitalocean_database_cluster.registry.name
}

output "host" {
  description = "Public host of the cluster."
  value       = digitalocean_database_cluster.registry.host
}

output "port" {
  description = "Cluster port."
  value       = digitalocean_database_cluster.registry.port
}

output "database" {
  description = "Application database name."
  value       = digitalocean_database_db.registry.name
}

output "user" {
  description = "Application DB user."
  value       = digitalocean_database_user.registry.name
}

output "password" {
  description = "Application DB user password. Print with `terraform output -raw password`."
  value       = digitalocean_database_user.registry.password
  sensitive   = true
}

# Ready-to-use connection string. Copy into .env as DATABASE_URL (and hand to
# collaborators out-of-band — never commit it). sslmode=require is mandatory for
# DO managed Postgres. Print with `terraform output -raw database_url`.
output "database_url" {
  description = "DATABASE_URL for the application DB user. Print with `terraform output -raw database_url`."
  value = format(
    "postgresql://%s:%s@%s:%d/%s?sslmode=require",
    digitalocean_database_user.registry.name,
    digitalocean_database_user.registry.password,
    digitalocean_database_cluster.registry.host,
    digitalocean_database_cluster.registry.port,
    digitalocean_database_db.registry.name,
  )
  sensitive = true
}
