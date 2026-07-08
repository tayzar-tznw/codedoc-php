<?php
declare(strict_types=1);

namespace App\S03_VendorTwins;

use Acme\Reporting\Report as AcmeReport;
use Globex\Reporting\Report as GlobexReport;

class DualReportRunner
{
    public function runBoth(): array
    {
        $acme = new AcmeReport();
        $globex = new GlobexReport();

        return [$acme->generate(), $globex->generate()];
    }
}
