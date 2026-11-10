from curl_cffi import requests

r = requests.post(
    "https://www.debijenkorf.nl/api/graphql",
    json={"query": 'query ProductStoreStock { productStoreStock(productVariantCode: "205108000026090") { status stock store { id name } } }'},
    impersonate="chrome",
)
print(r.status_code)
print(r.text)

