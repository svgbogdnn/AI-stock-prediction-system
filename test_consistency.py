#!/usr/bin/env python3
"""
Test script to verify recommendation consistency
Run multiple times with same ticker to verify deterministic behavior
"""

import sys
from agents.kaggle_orchestrator import KaggleOrchestrator

def test_consistency(ticker: str, num_runs: int = 3):
    """Run analysis multiple times and check consistency"""
    print(f"🧪 Testing Consistency for {ticker}")
    print(f"Running {num_runs} analyses...\n")
    print("=" * 70)
    
    orchestrator = KaggleOrchestrator()
    results = []
    
    for i in range(num_runs):
        print(f"\n📊 Run #{i+1}")
        print("-" * 70)
        result = orchestrator.analyze_stock(ticker)
        results.append(result)
    
    # Compare results
    print("\n" + "=" * 70)
    print("🔍 CONSISTENCY CHECK")
    print("=" * 70)
    
    # Check fundamental signals
    fund_signals = [r['analysis_reports']['fundamental']['directional_signal'] for r in results]
    print(f"\n💰 Fundamental Signals: {fund_signals}")
    if len(set(fund_signals)) == 1:
        print("   ✅ CONSISTENT - All runs identical")
    else:
        print("   ⚠️  VARIANCE DETECTED")
    
    # Check technical signals
    tech_signals = [r['analysis_reports']['technical']['directional_signal'] for r in results]
    print(f"\n📈 Technical Signals: {tech_signals}")
    if len(set(tech_signals)) == 1:
        print("   ✅ CONSISTENT - All runs identical")
    else:
        print("   ⚠️  VARIANCE DETECTED")
    
    # Check sentiment signals
    sent_signals = [r['analysis_reports']['sentiment']['directional_signal'] for r in results]
    print(f"\n📰 Sentiment Signals: {sent_signals}")
    if len(set(sent_signals)) == 1:
        print("   ✅ CONSISTENT - All runs identical")
    else:
        print("   ℹ️  EXPECTED - News data may change")
    
    # Check final recommendations
    recommendations = [r['recommendation'] for r in results]
    print(f"\n🎯 Final Recommendations: {recommendations}")
    if len(set(recommendations)) == 1:
        print("   ✅ CONSISTENT - All runs identical")
    else:
        print("   ⚠️  VARIANCE DETECTED - Check sentiment data")
    
    # Check weighted signals
    weighted_signals = [r['weighted_signal'] for r in results]
    print(f"\n⚖️  Weighted Signals: {weighted_signals}")
    signal_variance = max(weighted_signals) - min(weighted_signals)
    if signal_variance < 0.01:
        print(f"   ✅ HIGHLY CONSISTENT - Variance: {signal_variance:.4f}")
    elif signal_variance < 0.05:
        print(f"   ✅ ACCEPTABLE - Variance: {signal_variance:.4f}")
    else:
        print(f"   ⚠️  HIGH VARIANCE - {signal_variance:.4f}")
    
    print("\n" + "=" * 70)
    print("✅ Consistency test complete!")
    print("\nNote: Fundamental and Technical should be 100% consistent.")
    print("Sentiment may vary if news changes between runs.")
    print("=" * 70)

if __name__ == "__main__":
    ticker = sys.argv[1] if len(sys.argv) > 1 else "AAPL"
    runs = int(sys.argv[2]) if len(sys.argv) > 2 else 3
    
    test_consistency(ticker, runs)

