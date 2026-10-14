// Not deployed or validated. Review before use.
param location string = resourceGroup().location
param prefix string = 'pdoom'
param databricksHost string
param databricksClientId string
@secure()
param databricksClientSecret string
param warehouseId string
param ucCatalog string = 'workspace'
param ucSchema string = 'pdoom'
param image string = 'mcr.microsoft.com/k8se/quickstart:latest'

resource logs 'Microsoft.OperationalInsights/workspaces@2022-10-01' = {
  name: '${prefix}-logs'
  location: location
  properties: { sku: { name: 'PerGB2018' } }
}

resource acr 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: '${prefix}${uniqueString(resourceGroup().id)}'
  location: location
  sku: { name: 'Basic' }
  properties: { adminUserEnabled: true }
}

resource env 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: '${prefix}-env'
  location: location
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logs.properties.customerId
        sharedKey: logs.listKeys().primarySharedKey
      }
    }
  }
}

resource app 'Microsoft.App/containerApps@2024-03-01' = {
  name: '${prefix}-app'
  location: location
  properties: {
    managedEnvironmentId: env.id
    configuration: {
      ingress: { external: true, targetPort: 8000 }
      registries: [
        { server: acr.properties.loginServer, username: acr.listCredentials().username, passwordSecretRef: 'acr-password' }
      ]
      secrets: [
        { name: 'acr-password', value: acr.listCredentials().passwords[0].value }
        { name: 'databricks-secret', value: databricksClientSecret }
      ]
    }
    template: {
      // The session store is in memory, so exactly one replica.
      scale: { minReplicas: 1, maxReplicas: 1 }
      containers: [
        {
          name: 'app'
          image: image
          resources: { cpu: json('0.5'), memory: '1Gi' }
          env: [
            { name: 'DATABRICKS_HOST', value: databricksHost }
            { name: 'DATABRICKS_CLIENT_ID', value: databricksClientId }
            { name: 'DATABRICKS_CLIENT_SECRET', secretRef: 'databricks-secret' }
            { name: 'PDOOM_WAREHOUSE_ID', value: warehouseId }
            { name: 'PDOOM_UC_CATALOG', value: ucCatalog }
            { name: 'PDOOM_UC_SCHEMA', value: ucSchema }
            { name: 'PDOOM_SINK', value: 'sql' }
          ]
        }
      ]
    }
  }
}

output url string = 'https://${app.properties.configuration.ingress.fqdn}'
output registry string = acr.properties.loginServer
