import uvicorn
import os

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    print("=" * 60)
    print("  eSewa Automated Payment Gateway Bridge")
    print(f"  Checkout Demo:      http://localhost:{port}/")
    print(f"  Admin & Simulator:  http://localhost:{port}/admin")
    print(f"  API Docs (Swagger): http://localhost:{port}/docs")
    print("=" * 60)
    uvicorn.run(
        "esewa_gateway:app",
        host="0.0.0.0",
        port=port,
        reload=True
    )
