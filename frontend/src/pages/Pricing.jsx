import LandingNav from '../components/landing/LandingNav'
import PricingTable from '../components/landing/PricingTable'
import FAQ from '../components/landing/FAQ'
import LandingFooter from '../components/landing/LandingFooter'

export default function Pricing() {
  return (
    <div className="min-h-screen bg-[#0f1117] text-slate-200">
      <LandingNav />
      <div className="pt-24">
        <PricingTable />
        <FAQ />
        <LandingFooter />
      </div>
    </div>
  )
}
