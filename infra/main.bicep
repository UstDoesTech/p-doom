// The full p(doom) app (every player type) on Azure Container Apps, in the playground resource group.
// Reuses the registry (builtonplayground) and Container Apps environment of the
// Personal-UstOldfield-Built-On-Playground app; it creates a managed identity with AcrPull and the app.
// Not deployed or validated. Review before use.
param location string = resourceGroup().location
param prefix string = 'pdoom'
param acrName string = 'builtonplayground'
param environmentName string = 'managedEnvironment-rgpersonalnustu-b3e2'
param databricksHost string
param databricksClientId string
@secure()
param databricksClientSecret string
param warehouseId string
param ucCatalog string = 'pdoom'
param ucSchema string = 'pdoom'
@description('Full image reference, <registry>.azurecr.io/pdoom:<tag>. Build it with az acr build first.')
param image string

var acrPull = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '7f951dda-4ed3-4680-a7ca-43fe172d538d')

resource acr 'Microsoft.ContainerRegistry/registries@2023-07-01' existing = {
  name: acrName
}

resource env 'Microsoft.App/managedEnvironments@2024-03-01' existing = {
  name: environmentName
}

resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${prefix}-id'
  location: location
}

resource pull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(acr.id, identity.id, acrPull)
  scope: acr
  properties: {
    roleDefinitionId: acrPull
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource app 'Microsoft.App/containerApps@2024-03-01' = {
  name: '${prefix}-app'
  location: location
  dependsOn: [pull]
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${identity.id}': {} }
  }
  properties: {
    managedEnvironmentId: env.id
    configuration: {
      ingress: { external: true, targetPort: 8000 }
      registries: [{ server: acr.properties.loginServer, identity: identity.id }]
      secrets: [{ name: 'databricks-secret', value: databricksClientSecret }]
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
