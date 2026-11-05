// The human-only p(doom) on Azure Container Apps, in the playground resource group. It reuses the
// registry (builtonplayground) and the Container Apps environment that the
// Personal-UstOldfield-Built-On-Playground app already uses; it creates only a managed identity,
// its AcrPull role on that registry, and the app. Not deployed by the session that wrote it; CI
// runs `az bicep build` on it, which checks syntax only.
//
// Order: build the image into the registry first (`az acr build`), then deploy this with
// image = '<registry>/pdoom-human:<tag>'. The deploying identity needs rights to create role
// assignments (Owner or User Access Administrator on the resource group), since the app pulls
// from the registry as a managed identity with AcrPull.

@description('Must be the region of the existing environment and registry, which is not the resource group region (uksouth).')
param location string = 'westus2'
param prefix string = 'pdoomh'
@description('Existing registry in this resource group that the app pulls from.')
param acrName string = 'builtonplayground'
@description('Existing Container Apps environment in this resource group (shared with the playground app).')
param environmentName string = 'managedEnvironment-rgpersonalnustu-b3e2'
@description('Full image reference, <registry>.azurecr.io/pdoom-human:<tag>.')
param image string

param databricksHost string
param databricksClientId string
@secure()
param databricksClientSecret string

@description('projects/<id>/branches/<id>/endpoints/<id>')
param lakebaseEndpoint string = 'projects/pdoom/branches/production/endpoints/primary'
param pgHost string
param pgDatabase string = 'pdoom'

@description('Open games at once, per container. 0 uses the built-in default (300).')
param maxOpenSessions int = 0
@description('New games per minute from one address. 0 uses the built-in default (20).')
param sessionsPerMinute int = 0
param modelVersion string = 'dev'

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
      ingress: {
        external: true
        targetPort: 8000
        transport: 'auto'
        allowInsecure: false
      }
      registries: [{ server: acr.properties.loginServer, identity: identity.id }]
      secrets: [{ name: 'databricks-secret', value: databricksClientSecret }]
    }
    template: {
      // The session store is in memory, so exactly one replica; scaling out would lose games.
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
            { name: 'PDOOM_LAKEBASE_ENDPOINT', value: lakebaseEndpoint }
            { name: 'PDOOM_PG_HOST', value: pgHost }
            { name: 'PDOOM_PG_DATABASE', value: pgDatabase }
            { name: 'PDOOM_MAX_OPEN_SESSIONS', value: string(maxOpenSessions) }
            { name: 'PDOOM_SESSIONS_PER_MINUTE', value: string(sessionsPerMinute) }
            { name: 'PDOOM_MODEL_VERSION', value: modelVersion }
          ]
          probes: [
            {
              type: 'Liveness'
              httpGet: { path: '/api/health', port: 8000 }
              initialDelaySeconds: 10
              periodSeconds: 30
            }
            {
              type: 'Readiness'
              httpGet: { path: '/api/health', port: 8000 }
              periodSeconds: 10
            }
          ]
        }
      ]
    }
  }
}

output registry string = acr.name
output registryServer string = acr.properties.loginServer
output url string = 'https://${app.properties.configuration.ingress.fqdn}'
