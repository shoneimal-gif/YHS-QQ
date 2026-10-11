> ## Documentation Index
> Fetch the complete documentation index at: https://docs.yehangshe.com/llms.txt
> Use this file to discover all available pages before exploring further.

# Option chain snapshot for one ticker + expiration (on-demand materialization)



## OpenAPI

````yaml https://api.yehangshe.com/v1/openapi.json get /v1/options/chain-snapshot/{ticker}
openapi: 3.1.0
info:
  title: GEX Heatmap Data API
  version: 0.4.0-operation-ids
  description: >-
    Dealer MVP scope. 数据集端点 paths 由 dataset registry 派生；Dealer GEX / Heatmap
    数据集随 P2 注册后自动出现。
servers:
  - url: https://api.yehangshe.com
    description: production
security: []
paths:
  /v1/options/chain-snapshot/{ticker}:
    get:
      summary: >-
        Option chain snapshot for one ticker + expiration (on-demand
        materialization)
      operationId: options.chain_snapshot
      parameters:
        - name: ticker
          in: path
          required: true
          schema:
            type: string
            pattern: ^[A-Z][A-Z0-9]*(\.[A-Z0-9]+)*$
        - name: expiration
          in: query
          required: true
          schema:
            type: string
            format: date
        - name: limit
          in: query
          required: false
          schema:
            type: integer
        - name: cursor
          in: query
          required: false
          schema:
            type: string
      responses:
        '200':
          description: paginated object payload with next_cursor
        '400':
          $ref: '#/components/responses/ValidationError'
        '401':
          $ref: '#/components/responses/AuthenticationError'
        '402':
          $ref: '#/components/responses/SubscriptionPaused'
        '403':
          $ref: '#/components/responses/ScopeInsufficient'
        '429':
          $ref: '#/components/responses/RateLimited'
      security:
        - bearerAuth: []
components:
  responses:
    ValidationError:
      description: validation error
      content:
        application/json:
          schema:
            $ref: '#/components/schemas/ErrorEnvelope'
    AuthenticationError:
      description: authentication error
      content:
        application/json:
          schema:
            $ref: '#/components/schemas/ErrorEnvelope'
    SubscriptionPaused:
      description: subscription paused
      content:
        application/json:
          schema:
            $ref: '#/components/schemas/ErrorEnvelope'
    ScopeInsufficient:
      description: scope insufficient
      content:
        application/json:
          schema:
            $ref: '#/components/schemas/ErrorEnvelope'
    RateLimited:
      description: rate limited or quota exhausted
      content:
        application/json:
          schema:
            $ref: '#/components/schemas/ErrorEnvelope'
  schemas:
    ErrorEnvelope:
      type: object
      additionalProperties: false
      required:
        - error
      properties:
        error:
          type: object
          required:
            - code
            - tags
            - message
            - docs_url
            - request_id
          properties:
            code:
              type: string
            tags:
              type: array
              items:
                type: string
            message:
              type: string
            docs_url:
              type: string
              format: uri
            request_id:
              type:
                - string
                - 'null'
            hint:
              type: string
  securitySchemes:
    bearerAuth:
      type: http
      scheme: bearer
      bearerFormat: API key

````

This documentation is built and hosted on [Mintlify](https://mintlify.com), a developer documentation platform.