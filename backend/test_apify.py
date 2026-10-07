from pinterest import fetch_pinterest_images

results = fetch_pinterest_images("dark cyberpunk fashion", max_results=3)
print(f"\nGot {len(results)} images:")
for r in results:
    print(f"  url: {r['url']}")
    print(f"  title: {r['title']}")
    print()
