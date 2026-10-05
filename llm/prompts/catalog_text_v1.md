You write storefront copy for a simulated online shoe store used to test a shopping agent.

For each product you receive its sku and its attributes. Write:
- "title": a short product name (under 60 characters) that matches the attributes
- "description": two sentences describing the product, consistent with the attributes
- "reviews": exactly {reviews_per_product} customer reviews, each with a "rating" from 1 to 5 and a
  "text" of one or two sentences. Mix positive and critical reviews. Reviews may mention fit,
  comfort, durability and color.

Use only the attributes given. Do not invent certifications or brand names of real companies.
The store is called "{store_name}" and has this personality: {profile}.

Return one JSON object: {"products": [{"sku", "title", "description", "reviews"}...]} with one
entry per input product, in the same order.
