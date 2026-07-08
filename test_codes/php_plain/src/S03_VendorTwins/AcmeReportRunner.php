<?php
declare(strict_types=1);

namespace App\S03_VendorTwins;

use Acme\Reporting\Report;

class AcmeReportRunner
{
    public function run(): string
    {
        $report = new Report();

        return $report->generate();
    }
}
