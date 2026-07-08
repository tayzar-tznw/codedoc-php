<?php
declare(strict_types=1);

namespace App\S03_VendorTwins;

use Globex\Reporting\Report;

class GlobexReportRunner
{
    public function run(): string
    {
        $report = new Report();

        return $report->generate();
    }
}
