import LandingNav from '../components/landing/LandingNav'
import Hero from '../components/landing/Hero'
import SocialProof from '../components/landing/SocialProof'
import HowItWorks from '../components/landing/HowItWorks'
import ValidationStats from '../components/landing/ValidationStats'
import FeatureComparison from '../components/landing/FeatureComparison'
import PricingTable from '../components/landing/PricingTable'
import FAQ from '../components/landing/FAQ'
import LandingFooter from '../components/landing/LandingFooter'

export default function Landing() {
  return (
    <div className="min-h-screen bg-[#0f1117] text-slate-200">
      <LandingNav />
      <Hero />
      <SocialProof />
      <HowItWorks />
      <ValidationStats />
      <FeatureComparison />
      <PricingTable />
      <FAQ />
      <LandingFooter />
    </div>
  )
}
